import hashlib

from app.services.vector_store import query_chunks
from app.services.bm25_retriever import bm25_search

RRF_K = 60


def _chunk_key(source: str, page, text: str) -> str:
    # Hash the full text. A key built from the first 80 characters can collide
    # for chunks that share a section prefix, and a collision silently drops
    # one of them from the fused results.
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{source}|{page}|{digest}"


def reciprocal_rank_fusion(dense_results, bm25_results, top_k: int = 20):
    fused_scores = {}
    chunk_lookup = {}

    for rank, chunk in enumerate(dense_results):
        key = _chunk_key(chunk["source"], chunk["page"], chunk["text"])
        chunk_lookup.setdefault(key, chunk)
        fused_scores[key] = fused_scores.get(key, 0) + 1 / (RRF_K + rank + 1)

    for rank, chunk in enumerate(bm25_results):
        key = _chunk_key(chunk["source"], chunk["page"], chunk["text"])
        chunk_lookup.setdefault(key, chunk)
        fused_scores[key] = fused_scores.get(key, 0) + 1 / (RRF_K + rank + 1)

    ranked = sorted(fused_scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
    return [chunk_lookup[key] for key, _ in ranked]


def hybrid_search(query: str, chat_session_id: str, db, top_k: int = 20):
    dense_results = query_chunks(query, chat_session_id, top_k=20, db=db)
    bm25_results = bm25_search(query, chat_session_id, db, top_k=20)
    return reciprocal_rank_fusion(dense_results, bm25_results, top_k=top_k)