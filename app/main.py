import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from time import perf_counter

from app.db.database import Base, engine
from app.db.database import SessionLocal
from app.db import models
from app.api.auth import router as auth_router
from app.api.chats import router as chats_router
from app.api.chat import router as chat_router
from app.api.documents import router as documents_router
from app.config import MAX_UPLOAD_REQUEST_BYTES, get_allowed_origins, is_allowed_request_origin
from sqlalchemy import text as sql_text
from app.services.cleanup import cleanup_stale_guests


def _cleanup_expired_guest_data():
    db = SessionLocal()
    try:
        cleanup_stale_guests(db)
        cutoff = datetime.utcnow() - timedelta(hours=24)
        db.query(models.RateLimitBucket).filter(
            models.RateLimitBucket.window_start < cutoff
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


async def _guest_cleanup_loop():
    while True:
        try:
            await asyncio.to_thread(_cleanup_expired_guest_data)
        except Exception as e:
            # Keep the service available if a scheduled cleanup attempt fails.
            print(f"[guest_cleanup] Cleanup attempt failed ({type(e).__name__}).")
        await asyncio.sleep(60 * 60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cleanup_task = asyncio.create_task(_guest_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="PDF RAG Chatbot", lifespan=lifespan)

# Creates tables if they don't exist yet — fine at this scale.
# A real migration tool (Alembic) would replace this if the schema
# needs to change after data already exists.
Base.metadata.create_all(bind=engine)


def _ensure_chunk_session_index():
    """
    create_all() adds indexes only to NEW tables, so the index declared on
    DocumentChunk.chat_session_id is also created here for existing databases.
    Every search filters by chat session; without this index the whole chunk
    table is scanned.

    Deliberately a plain B-tree and NOT an HNSW vector index: an HNSW index
    applies the session filter after its approximate candidate search, which
    can return fewer than top_k results for a session with few chunks. A
    per-session B-tree lets Postgres compute exact distances over only that
    session's rows.
    """
    try:
        with engine.begin() as connection:
            connection.execute(sql_text(
                "CREATE INDEX IF NOT EXISTS ix_document_chunks_chat_session_id "
                "ON document_chunks (chat_session_id)"
            ))
    except Exception as e:
        print(f"[startup] Chunk session index not created ({type(e).__name__}); "
              f"searches still work, just slower.", flush=True)


_ensure_chunk_session_index()

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """
    Baseline security headers on every response — API and the
    StaticFiles-served frontend alike. None of these require any
    request-specific logic, so a single blanket middleware covers both.
    """
    is_upload = request.method == "POST" and request.url.path == "/api/documents/upload"
    request_started = perf_counter() if is_upload else None
    origin_rejected = False
    if request.method not in {"GET", "HEAD", "OPTIONS"} and (
        "access_token" in request.cookies or "guest_access" in request.cookies
    ):
        origin = request.headers.get("origin")
        if not is_allowed_request_origin(origin, str(request.base_url)):
            origin_rejected = True
    upload_too_large = False
    if is_upload:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                upload_too_large = int(content_length) > MAX_UPLOAD_REQUEST_BYTES
            except ValueError:
                upload_too_large = True
    try:
        if origin_rejected:
            response = JSONResponse({"detail": "Request origin is not allowed."}, status_code=403)
        elif upload_too_large:
            response = JSONResponse(
                {"detail": "This upload request exceeds the allowed size."},
                status_code=413,
            )
        else:
            response = await call_next(request)
    finally:
        if request_started is not None:
            duration = perf_counter() - request_started
            print(f"[documents.upload] server_request_total={duration:.3f}s")
    response.headers["X-Content-Type-Options"] = "nosniff"  # stop browsers from MIME-sniffing a response into executing as something it isn't
    response.headers["X-Frame-Options"] = "DENY"  # this app is never meant to be iframed elsewhere — blocks clickjacking
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"  # limits what leaks to third-party referrers on outbound links/requests
    return response


app.include_router(auth_router)
app.include_router(chats_router)
app.include_router(chat_router)
app.include_router(documents_router)


@app.get("/health")
def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")