import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def load_dataset(dataset_path: Path):
    with dataset_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def is_strict_match(chunk, expected_source, expected_page):
    return chunk["source"] == expected_source and chunk["page"] == expected_page


def is_keyword_match(chunk, expected_source, expected_keywords, min_ratio=0.5):
    """
    Secondary/looser check: same document, and at least half the
    expected keywords appear in the chunk text (case-insensitive).
    Used only for reporting alongside strict match, not to replace it.
    """
    if chunk["source"] != expected_source:
        return False
    text_lower = chunk["text"].lower()
    hits = sum(1 for kw in expected_keywords if kw.lower() in text_lower)
    return hits >= max(1, len(expected_keywords) * min_ratio)


def find_rank(chunks, item, match_fn):
    for i, chunk in enumerate(chunks, start=1):
        if match_fn(chunk, item):
            return i
    return None


def dense_retrieve(question, fetch_k, chat_session_id, db):
    """Use the same PostgreSQL/pgvector path as the application."""
    from app.services.vector_store import query_chunks

    return query_chunks(question, chat_session_id, top_k=fetch_k, db=db)


def evaluate(dataset, chat_session_id, db, top_k, retrieval_mode="dense", use_reranker=False, initial_k=20):
    """
    retrieval_mode: "dense" or "hybrid"
    use_reranker: whether to rerank the fetched candidates down to top_k
    """
    strict_ranks = []
    keyword_ranks = []
    strict_hits = 0
    keyword_hits = 0

    from app.services.hybrid_retriever import hybrid_search
    from app.services.reranker import rerank_chunks

    for item in dataset:
        fetch_k = initial_k if use_reranker else top_k

        if retrieval_mode == "hybrid":
            chunks = hybrid_search(item["question"], chat_session_id, db, top_k=fetch_k)
        else:
            chunks = dense_retrieve(item["question"], fetch_k, chat_session_id, db)

        if use_reranker:
            chunks = rerank_chunks(item["question"], chunks, top_k=top_k)
        else:
            chunks = chunks[:top_k]

        strict_rank = find_rank(
            chunks, item,
            lambda c, it: is_strict_match(c, it["expected_source"], it["expected_page"])
        )
        keyword_rank = find_rank(
            chunks, item,
            lambda c, it: is_keyword_match(c, it["expected_source"], it["expected_keywords"])
        )

        if strict_rank:
            strict_hits += 1
            strict_ranks.append(1 / strict_rank)
        else:
            strict_ranks.append(0)

        if keyword_rank:
            keyword_hits += 1
            keyword_ranks.append(1 / keyword_rank)
        else:
            keyword_ranks.append(0)

    n = len(dataset)
    return {
        "strict_recall": strict_hits / n,
        "strict_mrr": sum(strict_ranks) / n,
        "keyword_recall": keyword_hits / n,
        "keyword_mrr": sum(keyword_ranks) / n,
    }


def print_results(label, results, show_mrr):
    print(f"{label}:")
    print(f"  Strict match   -> Recall: {results['strict_recall']*100:.1f}%"
          + (f" | MRR: {results['strict_mrr']:.3f}" if show_mrr else ""))
    print(f"  Keyword-assist -> Recall: {results['keyword_recall']*100:.1f}%"
          + (f" | MRR: {results['keyword_mrr']:.3f}" if show_mrr else ""))
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate retrieval on an existing chat session.")
    parser.add_argument("--chat-session-id", required=True,
                        help="UUID of a chat containing the evaluation PDFs and indexed chunks")
    parser.add_argument("--dataset", type=Path, default=Path("evaluation/dataset.json"))
    parser.add_argument("--output", type=Path,
                        help="Optional path for JSON results; omitted means no file is written")
    args = parser.parse_args()

    dataset = load_dataset(args.dataset)
    print(f"Evaluating on {len(dataset)} questions...\n")
    all_results = {}
    from app.db.database import SessionLocal

    db = SessionLocal()
    try:
        for k in [5, 10]:
            combos = [
                ("DENSE (baseline)", "dense", False),
                ("DENSE + RERANKER", "dense", True),
                ("HYBRID (BM25 + dense, RRF)", "hybrid", False),
                ("HYBRID + RERANKER", "hybrid", True),
            ]
            for label, mode, use_reranker in combos:
                res = evaluate(dataset, args.chat_session_id, db, top_k=k,
                               retrieval_mode=mode, use_reranker=use_reranker)
                print_results(f"{label}, top-{k}", res, show_mrr=(k == 5))
                all_results[f"{label} | top-{k}"] = res
    finally:
        db.close()

    print("Note: 'Strict match' requires exact source+page match (narrow ground truth).")
    print("'Keyword-assist' counts chunks from the right document containing most expected keywords.")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
        print(f"\nFull results saved to {args.output}")
