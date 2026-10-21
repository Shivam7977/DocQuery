import secrets
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db import models
from app.schemas.auth import (
    SignupRequest, VerifyCodeRequest, LoginRequest, GoogleLoginRequest,
    SetPasswordRequest, ForgotPasswordRequest, ResetPasswordRequest, TokenResponse,
)
from app.services.security import hash_password, verify_password, create_access_token, create_guest_access_token, GUEST_ACCESS_TTL_HOURS, verify_google_token, generate_verification_code
from app.config import JWT_EXPIRE_MINUTES
from app.services.cleanup import cleanup_stale_guests
from app.services.email import send_welcome_email, send_verification_email, send_password_reset_email, send_google_only_reset_notice
from app.api.deps import get_current_user
from app.services.rate_limit import enforce_rate_limit

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_browser_access_token(response: Response, request: Request, user_id: str) -> TokenResponse:
    token = create_access_token(user_id)
    secure = request.url.scheme == "https"
    response.set_cookie(
        key="access_token",
        value=token,
        max_age=JWT_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    response.delete_cookie("guest_access", path="/", httponly=True, samesite="strict", secure=secure)
    # Keep the response token for existing non-browser API clients. The web
    # frontend intentionally discards it and relies on the HttpOnly cookie.
    return TokenResponse(access_token=token)

MAX_VERIFICATION_ATTEMPTS = 5
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15
MAX_RESET_ATTEMPTS = 5


@router.post("/signup")
def signup(payload: SignupRequest, request: Request, db: Session = Depends(get_db)):
    enforce_rate_limit(request, db, scope="signup", limit=5, window_seconds=900)
    existing = db.query(models.User).filter(models.User.email == payload.email).first()
    if existing and existing.is_verified:
        raise HTTPException(status_code=400, detail="An account with this email already exists.")

    code, expires_at = generate_verification_code()

    if existing:
        # Unverified leftover from a previous signup attempt — overwrite it
        # with the new password/code instead of creating a duplicate row.
        # Also reset verification_attempts: this is a fresh code, so a
        # previous run of guesses against the old code shouldn't count
        # against the new one.
        existing.password_hash = hash_password(payload.password)
        existing.verification_code = code
        existing.verification_code_expires_at = expires_at
        existing.verification_attempts = 0
        db.commit()
    else:
        user = models.User(
            email=payload.email,
            password_hash=hash_password(payload.password),
            is_verified=False,
            verification_code=code,
            verification_code_expires_at=expires_at,
        )
        db.add(user)
        db.commit()

    send_verification_email(payload.email, code)
    return {"message": "Verification code sent to your email."}


@router.post("/verify-signup", response_model=TokenResponse)
def verify_signup(payload: VerifyCodeRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    enforce_rate_limit(request, db, scope="verify_signup", limit=10, window_seconds=900)
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if not user or not user.verification_code:
        raise HTTPException(status_code=400, detail="No pending verification for this email.")

    if user.verification_code_expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="This code has expired. Please sign up again.")

    # SECURITY: brute-force protection — a 6-digit code is only ~1M
    # possibilities; without a cap, it's trivially guessable within the
    # 15-minute TTL. Once exhausted, the user must request a fresh code
    # (via /signup again) rather than keep guessing this one.
    if user.verification_attempts >= MAX_VERIFICATION_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many incorrect attempts. Please sign up again to get a new code.")

    # SECURITY: constant-time comparison — a plain `!=` leaks a timing
    # signal about how many leading characters matched, which is an
    # (admittedly minor, given the attempt-cap above) side-channel.
    if not secrets.compare_digest(user.verification_code, payload.code):
        user.verification_attempts += 1
        db.commit()
        raise HTTPException(status_code=400, detail="Incorrect code.")

    user.is_verified = True
    user.verification_code = None
    user.verification_code_expires_at = None
    user.verification_attempts = 0
    db.commit()

    send_welcome_email(user.email)

    return _issue_browser_access_token(response, request, str(user.id))


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    enforce_rate_limit(request, db, scope="login", limit=20, window_seconds=900)
    user = db.query(models.User).filter(models.User.email == payload.email).first()

    # SECURITY: brute-force protection on password guessing. Checked before
    # the password comparison itself so a locked-out account can't be
    # guessed against at all while locked.
    if user and user.lockout_until and user.lockout_until > datetime.utcnow():
        remaining_minutes = max(1, int((user.lockout_until - datetime.utcnow()).total_seconds() // 60) + 1)
        raise HTTPException(status_code=429, detail=f"Too many failed attempts. Try again in {remaining_minutes} minute(s).")

    if not user or not user.password_hash or not verify_password(payload.password, user.password_hash):
        if user:
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= MAX_LOGIN_ATTEMPTS:
                user.lockout_until = datetime.utcnow() + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
                user.failed_login_attempts = 0  # counter resets; lockout_until now gates further attempts
            db.commit()
        raise HTTPException(status_code=401, detail="Incorrect email or password.")

    if not user.is_verified:
        raise HTTPException(status_code=403, detail="Please verify your email before logging in.")

    user.failed_login_attempts = 0
    user.lockout_until = None
    db.commit()

    return _issue_browser_access_token(response, request, str(user.id))


@router.post("/google", response_model=TokenResponse)
def google_login(payload: GoogleLoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    enforce_rate_limit(request, db, scope="google_login", limit=10, window_seconds=900)
    try:
        info = verify_google_token(payload.id_token)
    except Exception as e:
        # Broadened from `except ValueError` — google-auth's verify_oauth2_token
        # can raise other exception types too (network/cert issues, its own
        # GoogleAuthError subclasses), which would otherwise surface as an
        # unhandled 500 with internal details instead of a clean 401.
        print(f"[auth.google_login] Token verification failed ({type(e).__name__}).")
        raise HTTPException(status_code=401, detail="Invalid Google token.")

    email, google_id = info["email"], info["sub"]
    user = db.query(models.User).filter(models.User.email == email).first()
    is_new_user = False
    if not user:
        # Google has already verified this email address itself.
        user = models.User(email=email, google_id=google_id, is_verified=True)
        db.add(user)
        db.commit()
        db.refresh(user)
        is_new_user = True
    elif not user.google_id:
        user.google_id = google_id
        user.is_verified = True
        db.commit()

    if is_new_user:
        send_welcome_email(user.email)

    return _issue_browser_access_token(response, request, str(user.id))


@router.post("/guest")
def start_guest_session(request: Request, response: Response, db: Session = Depends(get_db)):
    enforce_rate_limit(request, db, scope="guest_session", limit=10, window_seconds=900)
    cleanup_stale_guests(db)
    session = models.ChatSession(is_guest=True, user_id=None)
    db.add(session)
    db.commit()
    db.refresh(session)
    response.set_cookie(
        key="guest_access",
        value=create_guest_access_token(str(session.id)),
        max_age=GUEST_ACCESS_TTL_HOURS * 60 * 60,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    return {"chat_session_id": str(session.id)}


@router.post("/logout")
def logout(request: Request, response: Response):
    secure = request.url.scheme == "https"
    response.delete_cookie("access_token", path="/", httponly=True, samesite="strict", secure=secure)
    response.delete_cookie("guest_access", path="/", httponly=True, samesite="strict", secure=secure)
    return {"status": "logged out"}


@router.post("/set-password")
def set_password(
    payload: SetPasswordRequest,
    user: models.User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user:
        raise HTTPException(status_code=401, detail="Login required.")
    if user.password_hash:
        raise HTTPException(status_code=400, detail="This account already has a password. Use the change-password flow instead.")

    user.password_hash = hash_password(payload.password)
    db.commit()
    return {"status": "password set"}


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)):
    enforce_rate_limit(request, db, scope="forgot_password", limit=3, window_seconds=3600)
    """
    SECURITY: always returns the same generic response whether or not the
    email is registered (anti-enumeration) — the response never reveals
    account existence. Same principle for timing: both branches below do
    comparable work (a DB lookup either way), so no meaningful timing
    signal either.
    """
    user = db.query(models.User).filter(models.User.email == payload.email).first()

    if user and user.password_hash:
        # Reuse an existing still-valid code instead of rotating on every
        # request — repeated forgot-password requests for the same email
        # in quick succession shouldn't each trigger a fresh email send
        # (cost + spam-vector), and shouldn't reset an in-progress attempt
        # count either.
        if not user.reset_code or not user.reset_code_expires_at or user.reset_code_expires_at < datetime.utcnow():
            code, expires_at = generate_verification_code()
            user.reset_code = code
            user.reset_code_expires_at = expires_at
            user.reset_attempts = 0
            db.commit()
        send_password_reset_email(user.email, user.reset_code)
    elif user and not user.password_hash:
        # Google-only account — nothing to guess, so no code is generated.
        # Safe to be specific in the EMAIL here (only the account owner's
        # inbox receives it) even though the API response stays generic.
        send_google_only_reset_notice(user.email)
    # else: no account for this email — do nothing, still return the same
    # generic response below.

    return {"message": "If an account exists for this email, a reset code has been sent."}


@router.post("/reset-password", response_model=TokenResponse)
def reset_password(payload: ResetPasswordRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    enforce_rate_limit(request, db, scope="reset_password", limit=10, window_seconds=900)
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if not user or not user.reset_code:
        raise HTTPException(status_code=400, detail="Invalid or expired code.")

    if user.reset_code_expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="This code has expired. Please request a new one.")

    if user.reset_attempts >= MAX_RESET_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many incorrect attempts. Please request a new code.")

    if not secrets.compare_digest(user.reset_code, payload.code):
        user.reset_attempts += 1
        db.commit()
        raise HTTPException(status_code=400, detail="Incorrect code.")

    user.password_hash = hash_password(payload.new_password)
    user.reset_code = None
    user.reset_code_expires_at = None
    user.reset_attempts = 0
    db.commit()

    return _issue_browser_access_token(response, request, str(user.id))


@router.get("/me")
def get_current_user_info(user: models.User | None = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Login required.")
    return {
        "email": user.email,
        "has_password": user.password_hash is not None,
        "has_google": user.google_id is not None,
    }
