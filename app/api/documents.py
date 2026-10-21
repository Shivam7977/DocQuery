import threading
import time
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from time import perf_counter

from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Cookie, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db import models
from app.services.pdf_parser import extract_text_from_pdf
from app.services.table_extractor import extract_tables_from_pdf
from app.services.visual_extractor import extract_visuals_from_pdf
from app.services.chunker import chunk_pages
from app.services.vector_store import add_chunks
from app.services.bm25_retriever import invalidate_bm25_index
from app.services.cleanup import cleanup_stale_guests
from app.services.title_extractor import extract_display_title
from app.config import MAX_UPLOAD_BYTES_PER_SESSION, MAX_SINGLE_UPLOAD_BYTES
from app.api.deps import get_current_user, authorize_chat_session
from app.services.rate_limit import enforce_rate_limit

router = APIRouter()

UPLOAD_DIR = Path("data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_RESERVATION_TTL_HOURS = 6

# Live progress for the user-facing status line. Keyed by the client-generated
# upload_id. In-memory is fine: uploads run in this one process, and an entry
# is removed as soon as its upload finishes (or fails).
UPLOAD_PROGRESS_TTL_SECONDS = 2 * 3600
_upload_progress: dict[str, dict] = {}
_upload_progress_lock = threading.Lock()

# Plain-language labels. Internal stage names stay in the server logs only.
STAGE_LABELS = {
    "preflight": "Checking your file",
    "copy": "Receiving file",
    "quota_reservation": "Checking storage",
    "text_extraction": "Reading pages",
    "table_extraction": "Finding tables",
    "visual_extraction_and_description": "Analysing figures and charts",
    "chunking_and_embedding": "Indexing content",
    "title_generation": "Naming your document",
    "final_database_lookup": "Saving document",
    "document_file_read": "Saving document",
    "database_commit": "Saving document",
}


def _normalized_uuid(value: str) -> str:
    return str(uuid.UUID(str(value)))


def _set_progress(upload_id: str | None, stage: str, chat_session_id=None, detail: str | None = None):
    if not upload_id:
        return
    now = time.time()
    with _upload_progress_lock:
        entry = _upload_progress.get(upload_id)
        if entry is None:
            entry = {"chat_session_id": _normalized_uuid(chat_session_id), "started_at": now}
            _upload_progress[upload_id] = entry
        entry["stage"] = stage
        entry["detail"] = detail
        entry["updated_at"] = now
        # Drop entries from uploads that died without clearing themselves.
        cutoff = now - UPLOAD_PROGRESS_TTL_SECONDS
        for stale_id in [k for k, v in _upload_progress.items() if v["updated_at"] < cutoff]:
            del _upload_progress[stale_id]


def _clear_progress(upload_id: str | None):
    if not upload_id:
        return
    with _upload_progress_lock:
        _upload_progress.pop(upload_id, None)


def _reserve_upload_quota(db: Session, chat_session_id: str, user: models.User | None,
                          guest_access: str | None, request: Request, response: Response,
                          file_size: int) -> uuid.UUID:
    """Reserve session capacity under a row lock to serialize uploads."""
    session = (
        db.query(models.ChatSession)
        .filter(models.ChatSession.id == chat_session_id)
        .with_for_update()
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found.")
    authorize_chat_session(session, user, guest_access, request, response)

    # Reservations left by a process that crashed must not permanently consume quota.
    stale_cutoff = datetime.utcnow() - timedelta(hours=UPLOAD_RESERVATION_TTL_HOURS)
    db.query(models.UploadReservation).filter(
        models.UploadReservation.chat_session_id == chat_session_id,
        models.UploadReservation.created_at < stale_cutoff,
    ).delete(synchronize_session=False)

    reserved_bytes = (
        db.query(func.coalesce(func.sum(models.UploadReservation.size_bytes), 0))
        .filter(models.UploadReservation.chat_session_id == chat_session_id)
        .scalar()
    )
    if session.total_upload_bytes + reserved_bytes + file_size > MAX_UPLOAD_BYTES_PER_SESSION:
        db.rollback()
        raise HTTPException(
            status_code=413,
            detail="This chat has hit its 50MB storage limit. Start a new chat to upload more.",
        )

    reservation = models.UploadReservation(chat_session_id=session.id, size_bytes=file_size)
    db.add(reservation)
    db.commit()
    return reservation.id


def _release_upload_reservation(db: Session, reservation_id: uuid.UUID | None):
    if reservation_id is None:
        return
    try:
        db.rollback()
        reservation = db.query(models.UploadReservation).filter(
            models.UploadReservation.id == reservation_id
        ).first()
        if reservation:
            db.delete(reservation)
            db.commit()
    except Exception as e:
        db.rollback()
        # The six-hour expiry is a fallback if the cleanup write itself fails.
        print(f"[documents.upload] Reservation cleanup failed ({type(e).__name__}).")


@router.post("/api/documents/upload")
def upload_document(
    request: Request,
    response: Response,
    chat_session_id: str = Form(...),
    file: UploadFile = File(...),
    upload_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_current_user),
    guest_access: str | None = Cookie(default=None),
):
    upload_started = perf_counter()
    stage_durations = {}
    current_stage = "preflight"

    def begin_stage(name: str) -> float:
        nonlocal current_stage
        current_stage = name
        print(f"[documents.upload] stage={name} started", flush=True)
        _set_progress(upload_id, name, chat_session_id=chat_session_id)
        return perf_counter()

    stage_started = begin_stage("preflight")
    enforce_rate_limit(request, db, scope="document_upload", limit=6, window_seconds=3600)
    cleanup_stale_guests(db)

    # Validate BEFORE the DB query — an invalid UUID string reaching
    # Postgres as a query parameter raises a raw DataError (we saw this
    # exact failure mode ourselves debugging retrieval earlier), which
    # would otherwise surface as an unhandled 500 with internal details.
    try:
        uuid.UUID(chat_session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid chat session ID.")

    session = db.query(models.ChatSession).filter(models.ChatSession.id == chat_session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found. Start one via /auth/guest or /chats.")

    authorize_chat_session(session, user, guest_access, request, response)

    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")
    stage_durations["preflight"] = perf_counter() - stage_started

    # Stream the spooled upload to a server-named file in bounded blocks.
    # This avoids an extra full-file allocation before PDF processing.
    save_path = UPLOAD_DIR / f"{chat_session_id}_{uuid.uuid4().hex}.pdf"
    reservation_id = None
    try:
        stage_started = begin_stage("copy")
        file_size = 0
        signature = b""
        with open(save_path, "wb") as destination:
            while True:
                block = file.file.read(64 * 1024)
                if not block:
                    break
                if file_size == 0:
                    signature = block[:5]
                file_size += len(block)
                if file_size > MAX_SINGLE_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="This file exceeds the 20MB per-file limit.")
                destination.write(block)
        stage_durations["copy"] = perf_counter() - stage_started

        # MIME type is supplied by the client, so also check the actual PDF signature.
        if not signature.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="This file doesn't look like a valid PDF.")

        stage_started = begin_stage("quota_reservation")
        reservation_id = _reserve_upload_quota(
            db, chat_session_id, user, guest_access, request, response, file_size
        )
        stage_durations["quota_reservation"] = perf_counter() - stage_started

        from app.services.pdf_parser import PasswordProtectedPDFError, CorruptPDFError, PDFResourceLimitError
        stage_started = begin_stage("text_extraction")
        try:
            pages = extract_text_from_pdf(str(save_path), display_name=file.filename)
        except PasswordProtectedPDFError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except CorruptPDFError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except PDFResourceLimitError as e:
            raise HTTPException(status_code=413, detail=str(e))

        if not pages:
            raise HTTPException(status_code=422, detail="No extractable text found in this PDF.")
        print(f"[documents.upload] pages_with_text={len(pages)}", flush=True)
        _set_progress(upload_id, "text_extraction", chat_session_id=chat_session_id,
                      detail=f"{len(pages)} pages read")
        stage_durations["text_extraction"] = perf_counter() - stage_started

        # Best-effort natural-language title, so the document can be referred to
        # by topic and not just by its filename. It runs here, right after text
        # extraction and before the vision model is loaded, so the text LLM and
        # the vision model are not swapped in and out of Ollama around the figures.
        # It never blocks the upload on failure.
        stage_started = begin_stage("title_generation")
        display_title = extract_display_title(pages[0]["text"], fallback_filename=file.filename)
        stage_durations["title_generation"] = perf_counter() - stage_started

        # Best-effort: a PDF with no tables just yields an empty list here,
        # never blocks the upload on its own.
        stage_started = begin_stage("table_extraction")
        detected_table_regions = []
        table_chunks = extract_tables_from_pdf(
            str(save_path), display_name=file.filename, table_regions=detected_table_regions
        )
        print(f"[documents.upload] table_chunks={len(table_chunks)}", flush=True)
        stage_durations["table_extraction"] = perf_counter() - stage_started

        # Best-effort: unlike table extraction, this calls out to an
        # external vision model (Ollama locally, or Mistral in production)
        # — that call can genuinely fail for reasons outside our control
        # (Ollama not running, Mistral API/key issues, rate limits). A
        # figure/chart failing to describe itself should never block the
        # whole upload; the document is still useful without it.
        stage_started = begin_stage("visual_extraction_and_description")
        try:
            visual_chunks = extract_visuals_from_pdf(
                str(save_path), display_name=file.filename, table_regions=detected_table_regions,
                on_progress=lambda done, total: _set_progress(
                    upload_id, "visual_extraction_and_description",
                    chat_session_id=chat_session_id,
                    detail=f"Figure {done} of {total}",
                ),
            )
        except Exception as e:
            print(f"[visual_extractor] Image description failed ({type(e).__name__}).")
            visual_chunks = []
        print(f"[documents.upload] visual_chunks={len(visual_chunks)}", flush=True)
        stage_durations["visual_extraction_and_description"] = perf_counter() - stage_started

        stage_started = begin_stage("chunking_and_embedding")
        chunks = chunk_pages(pages, table_chunks=table_chunks, visual_chunks=visual_chunks)
        print(f"[documents.upload] chunks_prepared={len(chunks)}", flush=True)
        chunk_count = add_chunks(chunks, chat_session_id=chat_session_id, db=db)
        stage_durations["chunking_and_embedding"] = perf_counter() - stage_started

        # Re-fetch and lock the session for the short final transaction. The
        # reservation already accounts for this upload in concurrent quota checks.
        stage_started = begin_stage("final_database_lookup")
        session = (
            db.query(models.ChatSession)
            .filter(models.ChatSession.id == chat_session_id)
            .with_for_update()
            .first()
        )
        reservation = db.query(models.UploadReservation).filter(
            models.UploadReservation.id == reservation_id
        ).first()
        if not session or not reservation:
            raise HTTPException(status_code=409, detail="Upload reservation expired. Please try again.")
        stage_durations["final_database_lookup"] = perf_counter() - stage_started

        stage_started = begin_stage("document_file_read")
        document_bytes = save_path.read_bytes()
        stage_durations["document_file_read"] = perf_counter() - stage_started
        db.add(models.Document(
            chat_session_id=chat_session_id,
            filename=file.filename,
            display_title=display_title,
            content_type=file.content_type,
            data=document_bytes,
        ))

        session.total_upload_bytes += file_size
        session.last_active_at = datetime.utcnow()
        db.delete(reservation)
        stage_started = begin_stage("database_commit")
        db.commit()
        stage_durations["database_commit"] = perf_counter() - stage_started
        reservation_id = None
        invalidate_bm25_index(chat_session_id)
    except HTTPException:
        db.rollback()
        _release_upload_reservation(db, reservation_id)
        raise  # intended errors (422/413/etc. above) pass through unchanged
    except Exception as e:
        db.rollback()
        _release_upload_reservation(db, reservation_id)
        # Anything else is unexpected (a malformed PDF confusing pdfplumber,
        # a DB-write failure, etc.) — never leak the raw exception to the
        # client; log it server-side and return a clean, generic message.
        print(
            f"[documents.upload] Processing failed at stage={current_stage} "
            f"({type(e).__name__}).",
            flush=True,
        )
        raise HTTPException(status_code=500, detail="Something went wrong while processing this document. Please try again.")
    finally:
        _clear_progress(upload_id)
        save_path.unlink(missing_ok=True)
        stage_durations["endpoint_total"] = perf_counter() - upload_started
        rendered = ", ".join(f"{name}={duration:.3f}s" for name, duration in stage_durations.items())
        print(f"[documents.upload] stage durations: {rendered}")

    return {"filename": file.filename, "display_title": display_title, "chunks_indexed": chunk_count}


@router.get("/api/documents/upload/{upload_id}/progress")
def get_upload_progress(
    upload_id: str,
    chat_session_id: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_current_user),
    guest_access: str | None = Cookie(default=None),
):
    """
    Returns the current stage of an in-flight upload so the frontend can show
    it. Requires the same chat-session authorization as the upload itself, so
    one user cannot read another user's upload progress.
    """
    try:
        upload_key = _normalized_uuid(upload_id)
        session_key = _normalized_uuid(chat_session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid upload or chat session ID.")

    session = db.query(models.ChatSession).filter(models.ChatSession.id == session_key).first()
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found.")
    authorize_chat_session(session, user, guest_access, request, response)

    with _upload_progress_lock:
        entry = _upload_progress.get(upload_key)
        entry = dict(entry) if entry else None
    if not entry or entry["chat_session_id"] != session_key:
        # Not started yet (or already finished). The client keeps its current text.
        raise HTTPException(status_code=404, detail="No upload in progress.")

    stage = entry.get("stage", "preflight")
    return {
        "stage": stage,
        "label": STAGE_LABELS.get(stage, "Processing document"),
        "detail": entry.get("detail"),
        "elapsed_seconds": int(time.time() - entry["started_at"]),
    }