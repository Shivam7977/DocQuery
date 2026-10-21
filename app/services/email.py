import resend
from app.config import RESEND_API_KEY, FROM_EMAIL

resend.api_key = RESEND_API_KEY


def send_verification_email(to_email: str, code: str):
    """Not fire-and-forget like the welcome email — if this fails, signup
    should fail too, since the user can never verify without it."""
    resend.Emails.send({
        "from": FROM_EMAIL,
        "to": [to_email],
        "subject": "Verify your DocQuery account",
        "html": f"""
            <div style="font-family: sans-serif; max-width: 480px; margin: 0 auto;">
                <h2 style="color: #201C16;">Verify your email</h2>
                <p>Your verification code is:</p>
                <p style="font-size: 28px; font-weight: 600; letter-spacing: 4px;">{code}</p>
                <p style="color: #6B6255; font-size: 14px;">This code expires in 15 minutes.</p>
            </div>
        """,
    })


def send_welcome_email(to_email: str):
    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": [to_email],
            "subject": "Welcome to DocQuery",
            "html": """
                <div style="font-family: sans-serif; max-width: 480px; margin: 0 auto;">
                    <h2 style="color: #201C16;">Welcome to DocQuery</h2>
                    <p>Your account is ready. Upload a PDF and start asking it questions — every answer comes with the exact page it was drawn from.</p>
                </div>
            """,
        })
    except Exception as e:
        print(f"[email] Welcome email delivery failed ({type(e).__name__}).")


def send_password_reset_email(to_email: str, code: str):
    """
    Best-effort (unlike send_verification_email) — deliberately wrapped in
    try/except. /auth/forgot-password always returns the same generic
    response regardless of what happens here, to avoid leaking
    account-existence via a distinguishable error on email-send failure.
    The real failure is still logged server-side for debugging.
    """
    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": [to_email],
            "subject": "Reset your DocQuery password",
            "html": f"""
                <div style="font-family: sans-serif; max-width: 480px; margin: 0 auto;">
                    <h2 style="color: #201C16;">Reset your password</h2>
                    <p>Your password reset code is:</p>
                    <p style="font-size: 28px; font-weight: 600; letter-spacing: 4px;">{code}</p>
                    <p style="color: #6B6255; font-size: 14px;">This code expires in 15 minutes. If you didn't request this, you can safely ignore this email.</p>
                </div>
            """,
        })
    except Exception as e:
        print(f"[email] Password-reset email delivery failed ({type(e).__name__}).")


def send_google_only_reset_notice(to_email: str):
    """
    Sent instead of a reset code when a password-reset is requested for an
    account that has no password set (Google-only signup) — this is safe
    to send freely since it only ever reaches the actual account owner's
    inbox, unlike an API response (which is why /auth/forgot-password
    itself never reveals this distinction to the caller).
    """
    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": [to_email],
            "subject": "About your DocQuery account",
            "html": """
                <div style="font-family: sans-serif; max-width: 480px; margin: 0 auto;">
                    <h2 style="color: #201C16;">This account uses Google Sign-In</h2>
                    <p>Someone (hopefully you) requested a password reset for this email, but this DocQuery account doesn't have a password — it's set up to sign in with Google.</p>
                    <p style="color: #6B6255; font-size: 14px;">Just use "Continue with Google" to log in. If you'd like a password as well, you can add one from your account settings after signing in.</p>
                </div>
            """,
        })
    except Exception as e:
        print(f"[email] Account notice delivery failed ({type(e).__name__}).")
