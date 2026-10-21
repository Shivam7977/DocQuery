"""Index evaluation PDFs into an existing application chat session."""

import argparse
from datetime import datetime
from pathlib import Path
import sys
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_PDF_FILES = [
    PROJECT_ROOT / "data" / "uploads" / "22744iied.pdf",
    PROJECT_ROOT / "data" / "uploads" / "paper1.pdf",
    PROJECT_ROOT / "data" / "uploads" / "paper2.pdf",
]


def main():
    parser = argparse.ArgumentParser(
        description="Index evaluation PDFs into an existing PostgreSQL-backed chat session."
    )
    parser.add_argument(
        "--chat-session-id",
        required=True,
        help="UUID of an existing chat session to receive the evaluation documents",
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        action="append",
        help="PDF to index; repeat this option to supply multiple files. Defaults to the evaluation corpus paths.",
    )
    args = parser.parse_args()

    try:
        chat_session_id = UUID(args.chat_session_id)
    except ValueError:
        parser.error("--chat-session-id must be a valid UUID")

    pdf_files = args.pdf if args.pdf else DEFAULT_PDF_FILES
    missing = [path for path in pdf_files if not path.is_file()]
    if missing:
        parser.error("PDF file not found: " + ", ".join(str(path) for path in missing))

    from app.config import MAX_SINGLE_UPLOAD_BYTES, MAX_UPLOAD_BYTES_PER_SESSION
    from app.db import models
    from app.db.database import SessionLocal
    from app.services.bm25_retriever import invalidate_bm25_index
    from app.services.chunker import chunk_pages
    from app.services.embeddings import embed_texts
    from app.services.pdf_parser import extract_text_from_pdf

    db = SessionLocal()
    try:
        session = db.query(models.ChatSession).filter(
            models.ChatSession.id == chat_session_id
        ).first()
        if not session:
            parser.error("Chat session was not found in the configured database")

        file_sizes = {path: path.stat().st_size for path in pdf_files}
        oversized = [path for path, size in file_sizes.items() if size > MAX_SINGLE_UPLOAD_BYTES]
        if oversized:
            parser.error("PDF exceeds the 20 MB per-file limit: " + ", ".join(map(str, oversized)))
        total_size = sum(file_sizes.values())
        if session.total_upload_bytes + total_size > MAX_UPLOAD_BYTES_PER_SESSION:
            parser.error("These PDFs exceed the chat session's remaining 50 MB upload allowance")

        filenames = [path.name for path in pdf_files]
        if len(set(filenames)) != len(filenames):
            parser.error("PDF filenames must be unique within this ingestion run")
        existing = {
            row[0]
            for row in db.query(models.Document.filename)
            .filter(
                models.Document.chat_session_id == chat_session_id,
                models.Document.filename.in_(filenames),
            )
            .all()
        }
        if existing:
            parser.error(
                "Refusing to duplicate documents already in this chat: "
                + ", ".join(sorted(existing))
                + ". Use a fresh evaluation chat session."
            )

        prepared = []
        for path in pdf_files:
            file_bytes = path.read_bytes()
            if not file_bytes.startswith(b"%PDF-"):
                parser.error(f"File does not have a PDF signature: {path}")
            pages = extract_text_from_pdf(str(path), display_name=path.name)
            chunks = chunk_pages(pages)
            if not chunks:
                parser.error(f"No extractable text found in {path}")
            prepared.append((path, file_bytes, chunks))

        all_chunks = [chunk for _, _, chunks in prepared for chunk in chunks]
        embeddings = embed_texts([chunk["text"] for chunk in all_chunks])
        chunk_rows = [
            models.DocumentChunk(
                chat_session_id=chat_session_id,
                filename=chunk["source"],
                page=chunk["page"],
                text=chunk["text"],
                embedding=embedding,
                element_type=chunk.get("element_type", "text"),
                section_title=chunk.get("section_title"),
            )
            for chunk, embedding in zip(all_chunks, embeddings)
        ]

        db.add_all(chunk_rows)
        for path, file_bytes, _ in prepared:
            db.add(models.Document(
                chat_session_id=chat_session_id,
                filename=path.name,
                content_type="application/pdf",
                data=file_bytes,
            ))
        session.total_upload_bytes += total_size
        session.last_active_at = datetime.utcnow()
        db.commit()
        invalidate_bm25_index(str(chat_session_id))

        for path, _, chunks in prepared:
            print(f"Indexed {path.name}: {len(chunks)} chunks")
        print(f"Total indexed chunks: {len(chunk_rows)}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
