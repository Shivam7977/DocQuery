import os
import shutil
import logging
import pymupdf as fitz
import pytesseract
from PIL import Image
import io
from app.config import (
    MAX_OCR_PIXELS_PER_DOCUMENT,
    MAX_OCR_PIXELS_PER_PAGE,
    MAX_PDF_PAGES,
)

# Prefer an explicit deployment setting, then discover Tesseract on PATH.
# When neither is available, pytesseract will raise a clear missing-binary
# error which _ocr_page logs while allowing the rest of the PDF to proceed.
_TESSERACT_CMD = os.getenv("TESSERACT_CMD") or shutil.which("tesseract")
if _TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD

logger = logging.getLogger(__name__)
OCR_MIN_SELECTABLE_CHARS = 40

HEADING_SCORE_THRESHOLD = 3  # tune this after testing across a few PDFs


class PasswordProtectedPDFError(Exception):
    pass


class CorruptPDFError(Exception):
    pass


class PDFResourceLimitError(Exception):
    pass


def _get_body_font_size(page) -> float:
    sizes = {}
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                size = round(span["size"])
                sizes[size] = sizes.get(size, 0) + len(span["text"])
    if not sizes:
        return 10.0
    return max(sizes, key=sizes.get)


def _heading_score(text: str, avg_size: float, is_bold: bool, body_size: float) -> int:
    score = 0
    word_count = len(text.split())

    size_ratio = avg_size / body_size
    if size_ratio >= 1.3:
        score += 3
    elif size_ratio >= 1.15:
        score += 2
    elif size_ratio >= 1.05:
        score += 1

    if is_bold:
        score += 1

    if word_count <= 8:
        score += 1
    if word_count > 15:
        score -= 3

    stripped = text.rstrip()
    if stripped.endswith((".", ",", ";", ":")):
        score -= 2

    if text.isupper() and word_count <= 10:
        score += 1

    return score


def _extract_structured_text(page, body_size: float) -> str:
    blocks = [b for b in page.get_text("dict")["blocks"] if b.get("lines")]
    if not blocks:
        return ""

    page_width = page.rect.width
    midpoint = page_width / 2

    left_blocks = [b for b in blocks if b["bbox"][0] < midpoint]
    right_blocks = [b for b in blocks if b["bbox"][0] >= midpoint]
    is_two_column = len(left_blocks) >= 2 and len(right_blocks) >= 2

    if is_two_column:
        ordered_blocks = sorted(left_blocks, key=lambda b: b["bbox"][1]) + \
                          sorted(right_blocks, key=lambda b: b["bbox"][1])
    else:
        ordered_blocks = sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))

    scored_lines = []
    for block_index, block in enumerate(ordered_blocks):
        for line in block["lines"]:
            spans = line.get("spans", [])
            if not spans:
                continue
            line_text = "".join(s["text"] for s in spans).strip()
            if not line_text:
                continue

            avg_size = sum(s["size"] for s in spans) / len(spans)
            is_bold = any("bold" in s.get("font", "").lower() for s in spans)
            score = _heading_score(line_text, avg_size, is_bold, body_size)

            scored_lines.append({
                "text": line_text,
                "is_heading": score >= HEADING_SCORE_THRESHOLD,
                "block_index": block_index,
            })

    output_lines = []
    buffer = []
    buffer_block_index = None
    MAX_HEADING_WORDS = 20

    def flush_buffer():
        nonlocal buffer_block_index
        if buffer:
            merged_text = " ".join(buffer)
            if len(merged_text.split()) <= MAX_HEADING_WORDS:
                output_lines.append("# " + merged_text)
            else:
                output_lines.extend(buffer)
            buffer.clear()
        buffer_block_index = None

    for entry in scored_lines:
        if entry["is_heading"]:
            if buffer and entry["block_index"] != buffer_block_index:
                flush_buffer()
            buffer.append(entry["text"])
            buffer_block_index = entry["block_index"]
        else:
            flush_buffer()
            output_lines.append(entry["text"])
    flush_buffer()

    return "\n".join(output_lines)


def _ocr_page(page) -> str:
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    try:
        return pytesseract.image_to_string(img)
    except Exception as e:
        logger.warning("OCR could not process a PDF page (%s).", type(e).__name__)
        return ""


def extract_text_from_pdf(file_path: str, display_name: str | None = None):
    filename = display_name or file_path.split("\\")[-1].split("/")[-1]

    try:
        doc = fitz.open(file_path)
    except Exception:
        # Do not return parser internals or local paths to the caller.
        raise CorruptPDFError("Couldn't open this PDF — it may be corrupted.")

    if doc.needs_pass:
        doc.close()
        raise PasswordProtectedPDFError("This PDF is password-protected and can't be processed.")

    if doc.page_count > MAX_PDF_PAGES:
        doc.close()
        raise PDFResourceLimitError(
            f"This PDF has more than the {MAX_PDF_PAGES}-page processing limit."
        )

    extracted_pages = []
    ocr_pixels_used = 0
    try:
        for page_number, page in enumerate(doc, start=1):
            body_size = _get_body_font_size(page)
            text = _extract_structured_text(page, body_size)

            if len(text.strip()) < OCR_MIN_SELECTABLE_CHARS and len(page.get_images()) > 0:
                # Keep OCR at 2x for normal pages, but skip pathological page
                # dimensions or documents that exceed the total raster budget.
                estimated_pixels = int(page.rect.width * page.rect.height * 4)
                within_ocr_budget = (
                    estimated_pixels <= MAX_OCR_PIXELS_PER_PAGE
                    and ocr_pixels_used + estimated_pixels <= MAX_OCR_PIXELS_PER_DOCUMENT
                )
                if within_ocr_budget:
                    ocr_pixels_used += estimated_pixels
                    ocr_text = _ocr_page(page)
                    if ocr_text.strip():
                        text = "\n".join(part for part in (text.strip(), ocr_text.strip()) if part)
                else:
                    logger.warning("OCR skipped because the PDF exceeded its raster processing budget.")

            if text.strip():
                extracted_pages.append({
                    "text": text,
                    "page": page_number,
                    "source": filename
                })
    finally:
        doc.close()
    return extracted_pages


if __name__ == "__main__":
    import sys
    test_file = sys.argv[1] if len(sys.argv) > 1 else None
    if not test_file:
        print("Usage: python pdf_parser.py <path_to_pdf>")
    else:
        pages = extract_text_from_pdf(test_file)
        print(f"Extracted {len(pages)} pages with text.")
        print("--- Preview of page 1 ---")
        print(pages[0]["text"][:800] if pages else "No text found")
