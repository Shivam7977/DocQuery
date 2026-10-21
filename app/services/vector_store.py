from collections import defaultdict
from sqlalchemy.orm import Session
from app.db import models
from app.services.embeddings import embed_texts


def add_chunks(chunks: list, chat_session_id: str, db: Session) -> int:
    """Stage chunk rows in the caller's transaction; the caller commits."""
    texts = [c["text"] for c in chunks]
    embeddings = embed_texts(texts)

    rows = [
        models.DocumentChunk(
            chat_session_id=chat_session_id,
            filename=c["source"],
            page=c["page"],
            text=c["text"],
            embedding=emb,
            element_type=c.get("element_type", "text"),
            section_title=c.get("section_title"),
        )
        for c, emb in zip(chunks, embeddings)
    ]
    db.add_all(rows)
    return len(rows)


def query_chunks(query_text: str, chat_session_id: str, top_k: int, db: Session):
    query_embedding = embed_texts([query_text])[0]

    rows = (
        db.query(models.DocumentChunk)
        .filter(models.DocumentChunk.chat_session_id == chat_session_id)
        .order_by(models.DocumentChunk.embedding.l2_distance(query_embedding))
        .limit(top_k)
        .all()
    )
    return [
        {"text": r.text, "page": r.page, "source": r.filename, "element_type": r.element_type, "section_title": r.section_title}
        for r in rows
    ]


def get_all_chunks(chat_session_id: str, db: Session):
    rows = (
        db.query(models.DocumentChunk)
        .filter(models.DocumentChunk.chat_session_id == chat_session_id)
        .all()
    )
    return [
        {"text": r.text, "page": r.page, "source": r.filename, "chunk_id": str(r.id), "element_type": r.element_type, "section_title": r.section_title}
        for r in rows
    ]


def get_document_filenames(chat_session_id: str, db: Session) -> list[str]:
    rows = (
        db.query(models.DocumentChunk.filename)
        .filter(models.DocumentChunk.chat_session_id == chat_session_id)
        .distinct()
        .all()
    )
    return [row[0] for row in rows]


def get_documents_metadata(chat_session_id: str, db: Session) -> list[dict]:
    rows = (
        db.query(models.Document)
        .filter(models.Document.chat_session_id == chat_session_id)
        .all()
    )
    return [{"filename": r.filename, "display_title": r.display_title} for r in rows]


def count_documents(chat_session_id: str, db: Session) -> int:
    return len(get_document_filenames(chat_session_id, db))


def get_spread_chunks(chat_session_id: str, db: Session, chunks_per_document: int = 8):
    """
    Returns chunks evenly spread across pages, GROUPED per document (not
    flattened) — so a multi-document "summarize these" question can be
    answered per-document instead of blending every document's content
    into one confusing combined summary.

    Returns: [{"filename": ..., "chunks": [{"text","page","source"}, ...]}, ...]
    """
    rows = (
        db.query(models.DocumentChunk)
        .filter(models.DocumentChunk.chat_session_id == chat_session_id)
        .order_by(models.DocumentChunk.filename, models.DocumentChunk.page)
        .all()
    )
    if not rows:
        return []

    by_file = defaultdict(list)
    for r in rows:
        by_file[r.filename].append(r)

    grouped = []
    for filename, file_rows in by_file.items():
        n = len(file_rows)
        take = min(chunks_per_document, n)
        step = max(1, n // take)
        sampled = [file_rows[i] for i in range(0, n, step)][:chunks_per_document]
        grouped.append({
            "filename": filename,
            "chunks": [
                {"text": r.text, "page": r.page, "source": r.filename, "element_type": r.element_type, "section_title": r.section_title}
                for r in sampled
            ],
        })

    return grouped


def query_chunks_per_document(query_text: str, chat_session_id: str, db: Session, top_k_per_doc: int = 5, filenames: list[str] | None = None):
    """
    For "comparison" questions across multiple uploaded PDFs: runs dense
    retrieval SEPARATELY per filename, so every document contributes its
    own top matches — otherwise a single shared top-k could end up entirely
    dominated by whichever document happens to score highest overall.

    filenames: restrict to these specific documents only (used when the
    question explicitly names them). Pass None to include every document
    uploaded in this chat.
    """
    target_filenames = filenames if filenames else get_document_filenames(chat_session_id, db)

    query_embedding = embed_texts([query_text])[0]
    results = []
    for filename in target_filenames:
        rows = (
            db.query(models.DocumentChunk)
            .filter(
                models.DocumentChunk.chat_session_id == chat_session_id,
                models.DocumentChunk.filename == filename,
            )
            .order_by(models.DocumentChunk.embedding.l2_distance(query_embedding))
            .limit(top_k_per_doc)
            .all()
        )
        results.extend([
            {"text": r.text, "page": r.page, "source": r.filename, "element_type": r.element_type, "section_title": r.section_title}
            for r in rows
        ])

    return results

def get_distinct_section_titles(chat_session_id: str, db: Session) -> list[str]:
    """
    Returns every distinct, non-null section_title present in this session's
    chunks — the candidate list that a query gets matched against for the
    section-lookup shortcut (see query_classifier.match_section_title).
    """
    rows = (
        db.query(models.DocumentChunk.section_title)
        .filter(
            models.DocumentChunk.chat_session_id == chat_session_id,
            models.DocumentChunk.section_title.isnot(None),
        )
        .distinct()
        .all()
    )
    return [row[0] for row in rows]


def get_chunks_by_section(chat_session_id: str, section_title: str, db: Session,
                          limit: int | None = None):
    """
    Returns every chunk under an exact section_title — used by the
    section-lookup shortcut once match_section_title() has confidently
    identified which section a query is asking about. Bypasses embedding
    search and reranking entirely for this case; ordered by page so the
    section reads in document order.
    """
    query = (
        db.query(models.DocumentChunk)
        .filter(
            models.DocumentChunk.chat_session_id == chat_session_id,
            models.DocumentChunk.section_title == section_title,
        )
        .order_by(models.DocumentChunk.filename, models.DocumentChunk.page)
    )
    if limit is not None:
        query = query.limit(limit)
    rows = query.all()
    return [
        {"text": r.text, "page": r.page, "source": r.filename, "element_type": r.element_type, "section_title": r.section_title}
        for r in rows
    ]
