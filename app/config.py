import os
from urllib.parse import urlsplit
from dotenv import load_dotenv

load_dotenv()

# --- Database (Aiven Postgres) ---
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# --- Auth ---
JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "10080"))
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")

# --- Email (Resend) ---
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
FROM_EMAIL = os.getenv("FROM_EMAIL")

# --- Upload limits ---
MAX_UPLOAD_BYTES_PER_SESSION = 50 * 1024 * 1024
# Per-file cap, checked in addition to the per-session cumulative cap above.
# The frontend dropzone has always advertised "Max 20 MB" (see app.html) but
# the backend never actually enforced a per-file limit — only the 50MB
# session-total. Adding this closes that gap and matches what's promised.
MAX_SINGLE_UPLOAD_BYTES = 20 * 1024 * 1024
# Multipart framing and form fields are not part of the PDF byte count.
MAX_UPLOAD_REQUEST_BYTES = MAX_SINGLE_UPLOAD_BYTES + 1024 * 1024
# Bound parser work even when a small PDF contains a very large number of
# pages or oversized scanned pages. The OCR pixel budget is cumulative per
# upload, while the per-page cap bounds the largest single raster allocation.
MAX_PDF_PAGES = 300
MAX_OCR_PIXELS_PER_PAGE = 40_000_000
MAX_OCR_PIXELS_PER_DOCUMENT = 500_000_000

# --- CORS ---
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:8000")


def get_allowed_origins(frontend_origin: str | None = None) -> list[str]:
    """Return configured origins, treating local loopback names as aliases."""
    origins = {
        item.strip().rstrip("/")
        for item in (frontend_origin if frontend_origin is not None else FRONTEND_ORIGIN).split(",")
        if item.strip()
    }
    aliases = set(origins)
    for origin in origins:
        try:
            parsed = urlsplit(origin)
            host = (parsed.hostname or "").lower()
            if parsed.scheme not in {"http", "https"} or host not in {"localhost", "127.0.0.1"}:
                continue
            alternate_host = "127.0.0.1" if host == "localhost" else "localhost"
            port = f":{parsed.port}" if parsed.port is not None else ""
            aliases.add(f"{parsed.scheme}://{alternate_host}{port}")
        except ValueError:
            # Preserve the configured origin for normal validation to handle;
            # a malformed port should not prevent the app from starting.
            continue
    return sorted(aliases)


def is_allowed_request_origin(origin: str | None, request_origin: str) -> bool:
    """Allow configured frontend origins and the exact origin serving this app."""
    if not origin:
        return True
    normalized_origin = origin.rstrip("/")
    return normalized_origin == request_origin.rstrip("/") or normalized_origin in set(get_allowed_origins())

# --- LLM ---
# "ollama" for local dev, "groq" for production (Render can't run Ollama).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")


def validate_settings():
    """Fail early with actionable messages for settings required to run."""
    missing = [
        name
        for name, value in (("DATABASE_URL", DATABASE_URL), ("JWT_SECRET", JWT_SECRET))
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing)
            + ". Set them in the environment or in a local .env file."
        )

    if not DATABASE_URL.startswith(("postgres://", "postgresql://", "postgresql+")):
        raise RuntimeError(
            "DATABASE_URL must be a PostgreSQL connection URL "
            "(postgres:// or postgresql://)."
        )

    if LLM_PROVIDER not in {"ollama", "groq"}:
        raise RuntimeError("LLM_PROVIDER must be either 'ollama' or 'groq'.")
    if LLM_PROVIDER == "groq" and not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is required when LLM_PROVIDER=groq.")


validate_settings()
