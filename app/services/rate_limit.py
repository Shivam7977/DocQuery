import hashlib
import hmac
from datetime import datetime, timedelta

from fastapi import HTTPException, Request
from sqlalchemy import case
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import JWT_SECRET
from app.db.models import RateLimitBucket


def enforce_rate_limit(
    request: Request,
    db: Session,
    *,
    scope: str,
    limit: int,
    window_seconds: int,
) -> None:
    """Atomically enforce a per-client fixed window using PostgreSQL upsert."""
    client_ip = request.client.host if request.client else "unknown"
    subject_key = hmac.new(
        JWT_SECRET.encode("utf-8"), client_ip.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=window_seconds)
    statement = insert(RateLimitBucket).values(
        subject_key=subject_key,
        scope=scope,
        window_start=now,
        request_count=1,
    )
    statement = statement.on_conflict_do_update(
        constraint="uq_rate_limit_subject_scope",
        set_={
            "window_start": case(
                (RateLimitBucket.window_start <= cutoff, now),
                else_=RateLimitBucket.window_start,
            ),
            "request_count": case(
                (RateLimitBucket.window_start <= cutoff, 1),
                else_=RateLimitBucket.request_count + 1,
            ),
        },
    ).returning(RateLimitBucket.request_count, RateLimitBucket.window_start)

    try:
        count, active_window_start = db.execute(statement).one()
        db.commit()
    except Exception:
        db.rollback()
        raise

    if count > limit:
        retry_after = max(
            1,
            window_seconds - int((now - active_window_start).total_seconds()),
        )
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait and try again.",
            headers={"Retry-After": str(retry_after)},
        )
