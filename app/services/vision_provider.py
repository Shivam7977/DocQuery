"""
Vision provider abstraction for describing images/charts extracted from
PDFs. Two implementations share one interface so the rest of the pipeline
never cares which one is active:

  - OllamaVisionProvider — local (llava via Ollama), used in dev
  - MistralVisionProvider — hosted (Pixtral), used in production

Selected via the VISION_PROVIDER env var ("ollama" | "mistral").

Every describe call is isolated: a timeout, model error, or bad image
produces None for that one image and the batch continues.
"""
import io
import os
import base64
from abc import ABC, abstractmethod
from PIL import Image

VISION_MAX_SIDE_PX = int(os.getenv("VISION_MAX_SIDE_PX", "1024"))
VISION_TIMEOUT_SECONDS = float(os.getenv("VISION_TIMEOUT_SECONDS", "90"))
VISION_MAX_OUTPUT_TOKENS = int(os.getenv("VISION_MAX_OUTPUT_TOKENS", "200"))
VISION_KEEP_ALIVE = os.getenv("VISION_KEEP_ALIVE", "10m")

DESCRIPTION_PROMPT_TEMPLATE = """You are describing an image or chart extracted from a PDF document, for use in a \
retrieval system. Write a concise, factual description (3-5 sentences) that \
would let someone answer questions about this image WITHOUT seeing it.

- If it's a chart/graph: describe what is plotted, the overall trend, and \
preserve specific labels/numbers/axis values that are readable.
- If it's a photo/diagram: describe the key visual content and any labels.
- Do not speculate about anything not visibly present.
- Do not start with phrases like "This image shows" — just describe it directly.

{context_block}"""


def prepare_image_for_vision(image_bytes: bytes, max_side: int = VISION_MAX_SIDE_PX) -> bytes:
    """
    Downscale large images before sending them to a vision model. Vision
    encoders work at a fixed, fairly small input size, so pixels beyond
    that add decode/encode/transfer cost without adding information.
    Small images are returned untouched (no re-encoding).
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        if max(img.size) <= max_side:
            return image_bytes
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            flat = Image.alpha_composite(background, rgba).convert("RGB")
        else:
            flat = img.convert("RGB")
        flat.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        flat.save(buffer, "PNG")
        return buffer.getvalue()


class VisionProvider(ABC):
    @abstractmethod
    def describe(self, image_bytes: bytes, context: str | None = None) -> str | None:
        """Returns a text description, or None if the model gave no usable text."""
        raise NotImplementedError

    def _describe_safely(self, image_bytes: bytes, context: str | None, label: str) -> str | None:
        """One image, fully isolated: any failure returns None instead of raising."""
        try:
            prepared = prepare_image_for_vision(image_bytes)
            description = self.describe(prepared, context)
            return description or None
        except Exception as e:
            print(f"[vision] {label} failed ({type(e).__name__}), skipping this image's description.", flush=True)
            return None

    def describe_batch(self, images: list[bytes], contexts: list[str | None],
                       on_progress=None) -> list[str | None]:
        """Sequential by default. A local model should not be hit concurrently.
        on_progress(index, total) is called before each figure, for the UI."""
        descriptions = []
        total = min(len(images), len(contexts))
        for index, (image, context) in enumerate(zip(images, contexts), start=1):
            label = f"visual={index}/{total}"
            if on_progress:
                on_progress(index, total)
            print(f"[vision] {label} started", flush=True)
            descriptions.append(self._describe_safely(image, context, label))
            print(f"[vision] {label} finished", flush=True)
        return descriptions

    def _build_prompt(self, context: str | None) -> str:
        context_block = f"Nearby caption/text on the page: \"{context}\"" if context else ""
        return DESCRIPTION_PROMPT_TEMPLATE.format(context_block=context_block)


class OllamaVisionProvider(VisionProvider):
    def __init__(self, model: str | None = None):
        import ollama
        self.model = model or os.getenv("OLLAMA_VISION_MODEL", "llava")
        # httpx timeout: if Ollama sends no response within this many seconds,
        # the call raises and _describe_safely moves on to the next image.
        self._client = ollama.Client(timeout=VISION_TIMEOUT_SECONDS)

    def describe(self, image_bytes: bytes, context: str | None = None) -> str | None:
        response = self._client.chat(
            model=self.model,
            messages=[{
                "role": "user",
                "content": self._build_prompt(context),
                "images": [image_bytes],
            }],
            # Hard ceiling on generated tokens: a 3-5 sentence description
            # does not need unbounded output.
            options={"num_predict": VISION_MAX_OUTPUT_TOKENS},
            # Keep llava loaded between images, so each image doesn't pay the
            # model-load cost again. Also keeps it resident for the next upload.
            keep_alive=VISION_KEEP_ALIVE,
        )
        content = response["message"]["content"].strip()
        return content or None


class MistralVisionProvider(VisionProvider):
    def __init__(self, model: str | None = None, api_key: str | None = None):
        from mistralai import Mistral
        self.model = model or os.getenv("MISTRAL_VISION_MODEL", "pixtral-12b-2409")
        key = api_key or os.getenv("MISTRAL_API_KEY")
        if not key:
            raise RuntimeError("MISTRAL_API_KEY is not set.")
        self._client = Mistral(api_key=key)

    def _to_data_url(self, image_bytes: bytes) -> str:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:image/png;base64,{b64}"

    def describe(self, image_bytes: bytes, context: str | None = None) -> str | None:
        response = self._client.chat.complete(
            model=self.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": self._build_prompt(context)},
                    {"type": "image_url", "image_url": self._to_data_url(image_bytes)},
                ],
            }],
        )
        content = response.choices[0].message.content.strip()
        return content or None

    def describe_batch(self, images: list[bytes], contexts: list[str | None],
                       on_progress=None) -> list[str | None]:
        # Hosted API — safe to fan out. Each call is isolated by _describe_safely,
        # so one failed request cannot discard the others.
        import concurrent.futures

        total = min(len(images), len(contexts))

        def _call(args):
            index, image, context = args
            label = f"visual={index}/{total}"
            if on_progress:
                on_progress(index, total)
            print(f"[vision] {label} started", flush=True)
            description = self._describe_safely(image, context, label)
            print(f"[vision] {label} finished", flush=True)
            return description

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            return list(pool.map(_call, (
                (index, image, context)
                for index, (image, context) in enumerate(zip(images, contexts), start=1)
            )))


_PROVIDER_REGISTRY = {
    "ollama": OllamaVisionProvider,
    "mistral": MistralVisionProvider,
}


def get_vision_provider() -> VisionProvider:
    name = os.getenv("VISION_PROVIDER", "ollama").lower()
    if name not in _PROVIDER_REGISTRY:
        raise ValueError(f"Unknown VISION_PROVIDER '{name}'. Expected one of {list(_PROVIDER_REGISTRY)}.")
    return _PROVIDER_REGISTRY[name]()