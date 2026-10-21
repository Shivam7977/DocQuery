import json
from fastapi import APIRouter, Depends, HTTPException, Cookie, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from datetime import datetime

from app.db.database import get_db, SessionLocal
from app.db import models
from app.schemas.chat import ChatRequest
from app.services.retriever import retrieve_chunks
from app.services.llm import generate_answer, build_search_query, get_confidence_level
from app.api.deps import get_current_user, authorize_chat_session
from app.services.rate_limit import enforce_rate_limit

router = APIRouter()

RERANK_CONFIDENCE_THRESHOLD = -3.0
NO_ANSWER_MESSAGE = "I couldn't find this in the uploaded documents."
GENERATION_ERROR_MESSAGE = "Something went wrong while generating a response. Please try again."
HISTORY_LOOKBACK = 4


def sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _make_title_from_question(question: str) -> str:
    q = question.strip()
    return (q[:47] + "...") if len(q) > 50 else q


def _flatten_for_sources(chunks) -> list:
    """chunks can be a flat list (specific/comparison modes) or a grouped
    list of {"filename","chunks"} (broad mode) — normalize to flat for
    building the source-chips list either way."""
    if chunks and isinstance(chunks[0], dict) and "chunks" in chunks[0]:
        flat = []
        for group in chunks:
            flat.extend(group["chunks"])
        return flat
    return chunks


def stream_chat_response(question: str, chat_session_id):
    db = SessionLocal()
    try:
        recent_messages = (
            db.query(models.Message)
            .filter(models.Message.chat_session_id == chat_session_id)
            .order_by(models.Message.created_at.desc())
            .limit(HISTORY_LOOKBACK)
            .all()
        )
        recent_history = [
            {"role": m.role, "content": m.content}
            for m in reversed(recent_messages)
        ]

        search_query = build_search_query(question, recent_history)
        # Keep prompts and retrieved document text out of application logs.
        print("[DEBUG] query_prepared=true")
        chunks, mode = retrieve_chunks(search_query, question, chat_session_id, db, top_k=5)
        print(
            f"[DEBUG] mode={mode} retrieved_chunks={len(chunks)} "
            f"top_match_type={chunks[0].get('match_type', 'N/A') if chunks else 'none'}"
        )

        def save_message(role: str, content: str, sources_json: str):
            db.add(models.Message(chat_session_id=chat_session_id, role=role, content=content, sources_json=sources_json))
            session = db.query(models.ChatSession).filter(models.ChatSession.id == chat_session_id).first()
            if session:
                session.last_active_at = datetime.utcnow()
                if role == "user" and session.title in (None, "New chat"):
                    session.title = _make_title_from_question(content)
            db.commit()

        save_message("user", question, "[]")

        # A section-lookup chunk (match_type == "section_exact") never went
        # through the reranker, so it has no "rerank_score" key at all — it
        # bypassed that scoring entirely by design, on the strength of an
        # exact/normalized section-name match instead. Only a reranked
        # chunk's score is checked against the threshold; a section-exact
        # top chunk is always treated as present/answerable.
        top_chunk = chunks[0] if chunks else None
        is_section_match = bool(top_chunk) and top_chunk.get("match_type") == "section_exact"

        no_content = not chunks or (
            mode == "specific"
            and not is_section_match
            and top_chunk["rerank_score"] < RERANK_CONFIDENCE_THRESHOLD
        )

        if no_content:
            for word in NO_ANSWER_MESSAGE.split(" "):
                yield sse_event("token", {"content": word + " "})
            yield sse_event("done", {})
            yield sse_event("sources", {"sources": []})
            save_message("assistant", NO_ANSWER_MESSAGE, "[]")
            return

        if mode == "specific":
            confidence = "high" if is_section_match else get_confidence_level(top_chunk["rerank_score"])
        else:
            confidence = None

        # Drop weak-scoring chunks before they ever reach the LLM — but only
        # ones that actually carry a rerank_score to filter on. Section-
        # lookup chunks (match_type == "section_exact") never went through
        # reranking and have no rerank_score key at all, so they're always
        # kept as-is; filtering only applies to the normal reranked path. A
        # small local/free-tier model can get confused and hedge into
        # "couldn't find this" when it sees several unrelated sections mixed
        # into the same context, even if the top chunk clearly answers the
        # question — telling it via prompt instruction to "ignore irrelevant
        # sections" wasn't reliable enough on its own, so filter at the
        # source instead.
        if mode == "specific" and not is_section_match:
            filtered_chunks = [c for c in chunks if c["rerank_score"] >= RERANK_CONFIDENCE_THRESHOLD]
        else:
            filtered_chunks = chunks

        print(f"[DEBUG] chunks_sent_to_llm={len(filtered_chunks)}")

        # Broad-mode retrieval (get_spread_chunks) never consumes
        # search_query — it just spreads chunks across the whole document
        # regardless of query text — so use the original question there,
        # never a templated/resolved version.
        #
        # For a section-lookup match, retrieval already confidently
        # identified WHICH section answers this — don't make the LLM
        # re-derive that from a templated/singular query like "What does
        # the document say about skill?" (which can make a small model
        # answer too literally/narrowly). Tell it directly which section to
        # draw from instead. This is fully generic — it works off whatever
        # section_title actually is in THIS document (Education,
        # Introduction, Payment Details, Methodology, ...), never a
        # hardcoded, document-type-specific word.
        #
        # Otherwise (normal reranked path), search_query is used as before.
        if mode == "broad":
            generation_query = question
        elif is_section_match:
            if top_chunk.get("context_truncated"):
                generation_query = (
                    f"Answer from the available excerpt of the '{top_chunk['section_title']}' section. "
                    "The excerpt may not include the full section, so do not imply that it does."
                )
            else:
                generation_query = f"Provide a complete answer using the '{top_chunk['section_title']}' section."
        else:
            generation_query = search_query

        answer_text = ""
        try:
            for token in generate_answer(generation_query, filtered_chunks, mode=mode):
                answer_text += token
                yield sse_event("token", {"content": token})

            yield sse_event("done", {})

            flat_chunks = _flatten_for_sources(filtered_chunks)
            seen = set()
            sources_out = []
            for c in flat_chunks:
                key = (c["source"], c["page"])
                if key in seen:
                    continue
                seen.add(key)
                sources_out.append({"page": c["page"], "source": c["source"], "text": c["text"][:200]})

            sources_payload = {"sources": sources_out}
            if confidence:
                sources_payload["confidence"] = confidence
            yield sse_event("sources", sources_payload)
            save_message("assistant", answer_text, json.dumps(sources_out))

        except Exception as e:
            # SECURITY: never leak the raw exception to the client — it can
            # contain internal details (file paths, DB/library internals,
            # provider error bodies). Log the real error server-side for
            # debugging; the client only ever sees a generic message.
            print(f"[chat.stream_chat_response] Generation failed ({type(e).__name__}).")
            yield sse_event("error", {"message": GENERATION_ERROR_MESSAGE})
            save_message("assistant", GENERATION_ERROR_MESSAGE, "[]")
    finally:
        db.close()


@router.post("/chat")
def chat(
    request: ChatRequest,
    http_request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: models.User | None = Depends(get_current_user),
    guest_access: str | None = Cookie(default=None),
):
    enforce_rate_limit(http_request, db, scope="chat", limit=30, window_seconds=60)
    session = db.query(models.ChatSession).filter(models.ChatSession.id == request.chat_session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found. Start one via /auth/guest or /chats.")

    authorize_chat_session(session, user, guest_access, http_request, response)

    return StreamingResponse(
        # Validated as UUID by Pydantic above (malformed input already
        # rejected with a clean 422) — converted back to str here so it
        # stays consistent with documents.py's string-typed chat_session_id
        # (Form field), since bm25_retriever's in-memory cache is keyed by
        # plain strings. A UUID object and its string form don't hash equal
        # in Python, so mixing the two types across entry points would
        # silently break cache invalidation.
        stream_chat_response(request.question, str(request.chat_session_id)),
        media_type="text/event-stream",
    )
