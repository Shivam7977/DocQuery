from app.services.hybrid_retriever import hybrid_search
from app.services.reranker import rerank_chunks
from app.services.vector_store import (
    get_spread_chunks, query_chunks_per_document, count_documents,
    get_documents_metadata, get_distinct_section_titles, get_chunks_by_section,
)
from app.services.query_classifier import classify_question, extract_mentioned_documents, match_section_title

MAX_SECTION_CONTEXT_CHUNKS = 20


def retrieve_chunks(search_query: str, original_question: str, chat_session_id: str, db, top_k: int = 5, initial_k: int = 20):
    """
    search_query: the (possibly templated) query used for actual retrieval.
    original_question: the user's raw, unmodified question — used for mode
        classification, document-mention matching, and section-title
        matching (never the templated version, which could be a bare
        keyword wrapped in "What does the document say about X?" phrasing
        that would never match a section title as cleanly as the raw
        question would).

    Returns (chunks, mode).
    - "specific": flat list of chunks. Each chunk carries either a
      "rerank_score" (normal hybrid+rerank path) OR a "match_type" of
      "section_exact" (section-lookup shortcut path) — never both. Callers
      must check which key is present rather than assuming rerank_score
      always exists.
    - "broad": list of {"filename", "chunks"} GROUPED per document.
    - "comparison": flat list of chunks (each carries its own "source").
    """
    document_count = count_documents(chat_session_id, db)
    mode = classify_question(original_question, document_count)

    if mode == "broad":
        return get_spread_chunks(chat_session_id, db, chunks_per_document=8), mode

    if mode == "comparison":
        documents = get_documents_metadata(chat_session_id, db)
        mentioned = extract_mentioned_documents(original_question, documents)
        target_filenames = mentioned if len(mentioned) >= 2 else None
        return query_chunks_per_document(search_query, chat_session_id, db, top_k_per_doc=5, filenames=target_filenames), mode

    # Section-lookup shortcut: for a query that's basically a section name
    # ("skills", "education", "Positions of Responsibility"), the
    # cross-encoder reranker scores it poorly even when retrieval finds the
    # exact right chunk — bare section-name queries don't read like the
    # question<->passage pairs it was trained on. Bypass hybrid search +
    # reranking entirely when we can confidently identify the section.
    section_titles = get_distinct_section_titles(chat_session_id, db)
    matched_section = match_section_title(original_question, section_titles)
    if matched_section:
        section_chunks = get_chunks_by_section(
            chat_session_id,
            matched_section,
            db,
            limit=MAX_SECTION_CONTEXT_CHUNKS + 1,
        )
        context_truncated = len(section_chunks) > MAX_SECTION_CONTEXT_CHUNKS
        section_chunks = section_chunks[:MAX_SECTION_CONTEXT_CHUNKS]
        for c in section_chunks:
            c["match_type"] = "section_exact"
        if context_truncated and section_chunks:
            section_chunks[0]["context_truncated"] = True
        return section_chunks, mode

    chunks = hybrid_search(search_query, chat_session_id, db, top_k=initial_k)
    reranked_chunks = rerank_chunks(search_query, chunks, top_k=top_k)
    for c in reranked_chunks:
        c["match_type"] = "reranked"
    return reranked_chunks, mode
