from uuid import UUID
from fastapi import Cookie, Depends, Header, HTTPException, Request, Response
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.db import models
from app.services.security import (
    GUEST_ACCESS_TTL_HOURS,
    create_guest_access_token,
    decode_access_token,
    decode_guest_access_token,
)
from app.config import is_allowed_request_origin


def get_current_user(
    authorization: str | None = Header(default=None),
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> models.User | None:
    """Returns the logged-in user from a bearer token or browser cookie."""
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
    elif access_token:
        token = access_token
    if not token:
        return None
    user_id = decode_access_token(token)
    if not user_id:
        return None
    try:
        user_uuid = UUID(user_id)
    except ValueError:
        return None
    return db.query(models.User).filter(models.User.id == user_uuid).first()


def authorize_chat_session(
    session: models.ChatSession,
    user: models.User | None,
    guest_access: str | None,
    request: Request,
    response: Response,
):
    """Authorize an owned chat by account or a guest chat by its HttpOnly credential."""
    if not session:
        raise HTTPException(status_code=404, detail="Chat not found.")

    origin = request.headers.get("origin")
    if not is_allowed_request_origin(origin, str(request.base_url)):
        raise HTTPException(status_code=403, detail="Request origin is not allowed.")

    if session.user_id is not None:
        if not user or session.user_id != user.id:
            raise HTTPException(status_code=403, detail="This chat belongs to another account.")
        return

    if not session.is_guest or not guest_access:
        raise HTTPException(status_code=403, detail="Guest chat access is missing or expired. Start a new guest chat.")

    token_session_id = decode_guest_access_token(guest_access)
    if token_session_id != str(session.id):
        raise HTTPException(status_code=403, detail="Guest chat access is missing or expired. Start a new guest chat.")

    # SameSite cookies block cross-site requests; this origin check also
    # blocks requests from a hostile sibling origin on the same site.
    # Refresh the short-lived capability on every successful guest request,
    # matching the guest-session inactivity lifetime.
    response.set_cookie(
        key="guest_access",
        value=create_guest_access_token(str(session.id)),
        max_age=GUEST_ACCESS_TTL_HOURS * 60 * 60,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
