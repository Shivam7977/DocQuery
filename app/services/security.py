import secrets
from datetime import datetime, timedelta, timezone
from passlib.context import CryptContext
from jose import jwt, JWTError
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from google.auth.exceptions import GoogleAuthError

from app.config import JWT_SECRET, JWT_ALGORITHM, JWT_EXPIRE_MINUTES, GOOGLE_CLIENT_ID

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

VERIFICATION_CODE_TTL_MINUTES = 15
MAX_VERIFICATION_ATTEMPTS = 5
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def create_access_token(user_id: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=JWT_EXPIRE_MINUTES)
    return jwt.encode({"sub": user_id, "exp": expire}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


GUEST_ACCESS_TTL_HOURS = 24


def create_guest_access_token(chat_session_id: str) -> str:
    """Create a short-lived credential bound to one temporary guest chat."""
    expire = datetime.now(timezone.utc) + timedelta(hours=GUEST_ACCESS_TTL_HOURS)
    return jwt.encode(
        {"sub": chat_session_id, "typ": "guest", "exp": expire},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def decode_guest_access_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("typ") != "guest":
            return None
        return payload.get("sub")
    except JWTError:
        return None


def verify_google_token(token: str) -> dict:
    """
    Raises ValueError for a malformed/invalid/wrong-audience token,
    and google.auth.exceptions.GoogleAuthError (e.g. TransportError) for
    network-level failures while contacting Google's verification endpoint.
    Callers should catch (ValueError, GoogleAuthError).
    """
    return google_id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)


def generate_verification_code() -> tuple[str, "datetime"]:
    """Returns a 6-digit numeric code (plaintext, to email) and its expiry timestamp."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = datetime.utcnow() + timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES)
    return code, expires_at


def hash_verification_code(code: str) -> str:
    """Codes are stored hashed (bcrypt) — never store the plaintext code in the DB."""
    return pwd_context.hash(code)


def verify_verification_code(code: str, code_hash: str) -> bool:
    return pwd_context.verify(code, code_hash)
