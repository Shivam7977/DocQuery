import json
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Response, Cookie, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.db.database import get_db
from app.db import models
from app.api.deps import get_current_user, authorize_chat_session
from app.services.bm25_retriever import invalidate_bm25_index

router = APIRouter(prefix="/chats", tags=["chats"])


class RenameChatRequest(BaseModel):
    title: str


def _check_ownership(session: models.ChatSession, user: models.User | None,
                     guest_access: str | None, request: Request, response: Response):
    authorize_chat_session(session, user, guest_access, request, response)


@router.post("")
def create_chat(db: Session = Depends(get_db), user: models.User | None = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Login required to create a saved chat. Use /auth/guest for a temporary one.")
    session = models.ChatSession(is_guest=False, user_id=user.id)
    db.add(session)
    db.commit()
    db.refresh(session)
    return {"chat_session_id": str(session.id)}


@router.get("")
def list_chats(db: Session = Depends(get_db), user: models.User | None = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Login required.")
    sessions = (
        db.query(models.ChatSession)
        .filter(models.ChatSession.user_id == user.id)
        .order_by(models.ChatSession.last_active_at.desc())
        .all()
    )
    return [
        {
            "id": str(s.id),
            "title": s.title,
            "created_at": s.created_at.isoformat() + "Z",
            "last_active_at": s.last_active_at.isoformat() + "Z",
        }
        for s in sessions
    ]


# NOTE: chat_session_id is typed as UUID (not str) on every route below —
# FastAPI validates the path-param format before the handler ever runs, so
# a malformed ID gets a clean 422 instead of reaching a DB query raw (same
# fix already applied to schemas/chat.py). Converted to str() only at the
# specific call site that needs it (invalidate_bm25_index, which is keyed
# by plain strings — see the note in chat.py).

@router.get("/{chat_session_id}")
def get_chat(chat_session_id: UUID, request: Request, response: Response, db: Session = Depends(get_db), user: models.User | None = Depends(get_current_user), guest_access: str | None = Cookie(default=None)):
    session = db.query(models.ChatSession).filter(models.ChatSession.id == chat_session_id).first()
    _check_ownership(session, user, guest_access, request, response)

    messages = (
        db.query(models.Message)
        .filter(models.Message.chat_session_id == chat_session_id)
        .order_by(models.Message.created_at.asc())
        .all()
    )

    doc_rows = (
        db.query(models.DocumentChunk.filename)
        .filter(models.DocumentChunk.chat_session_id == chat_session_id)
        .distinct()
        .all()
    )
    documents = [row[0] for row in doc_rows]

    return {
        "id": str(session.id),
        "title": session.title,
        "created_at": session.created_at.isoformat() + "Z",
        "has_documents": len(documents) > 0,
        "documents": documents,
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "sources": json.loads(m.sources_json) if m.sources_json else [],
                "created_at": m.created_at.isoformat() + "Z",
            }
            for m in messages
        ],
    }


@router.patch("/{chat_session_id}")
def rename_chat(chat_session_id: UUID, payload: RenameChatRequest, request: Request, response: Response, db: Session = Depends(get_db), user: models.User | None = Depends(get_current_user), guest_access: str | None = Cookie(default=None)):
    session = db.query(models.ChatSession).filter(models.ChatSession.id == chat_session_id).first()
    _check_ownership(session, user, guest_access, request, response)

    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title can't be empty.")

    session.title = title[:100]
    db.commit()
    return {"title": session.title}


@router.get("/{chat_session_id}/documents/{filename}")
def get_document_file(chat_session_id: UUID, filename: str, request: Request, response: Response, db: Session = Depends(get_db), user: models.User | None = Depends(get_current_user), guest_access: str | None = Cookie(default=None)):
    session = db.query(models.ChatSession).filter(models.ChatSession.id == chat_session_id).first()
    _check_ownership(session, user, guest_access, request, response)

    doc = (
        db.query(models.Document)
        .filter(models.Document.chat_session_id == chat_session_id, models.Document.filename == filename)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found in this chat.")

    return Response(content=doc.data, media_type=doc.content_type)


@router.delete("/{chat_session_id}")
def delete_chat(chat_session_id: UUID, request: Request, response: Response, db: Session = Depends(get_db), user: models.User | None = Depends(get_current_user), guest_access: str | None = Cookie(default=None)):
    session = db.query(models.ChatSession).filter(models.ChatSession.id == chat_session_id).first()
    _check_ownership(session, user, guest_access, request, response)

    db.delete(session)
    db.commit()

    # Was missing before — the in-memory BM25 index for this session would
    # otherwise outlive the (now-deleted) session's data until server
    # restart, wasting memory indefinitely on a long-running server.
    invalidate_bm25_index(str(chat_session_id))

    return {"status": "deleted"}
