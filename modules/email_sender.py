"""Transactional email sender via Resend HTTPS API.

Railway blocks outbound SMTP on every standard port (25/465/587), so the
previous Gmail-SMTP implementation never delivered from production. Resend
ships over HTTPS, sidesteps the block, and gives us proper SPF/DKIM once a
sending domain is verified.

The send_email() signature is intentionally unchanged from the SMTP version
so callers don't need to change.
"""

import sys

import requests

from config import RESEND_API_KEY, RESEND_FROM_EMAIL

_RESEND_URL = "https://api.resend.com/emails"
# Resend's shared sandbox sender — works without a verified domain but only
# delivers to the email address verified on the Resend account. Swap to a
# noreply@your-domain.com once the sending domain is verified.
_DEFAULT_FROM = "onboarding@resend.dev"


def send_email(to: str, subject: str, html_body: str) -> bool:
    """Send a single HTML email via Resend. True on success, False on failure.

    All failure paths log to stderr with the `[CRITICAL email]` prefix so they
    are greppable in Railway logs (`railway logs --service web | grep CRITICAL`).
    """
    if not RESEND_API_KEY:
        print("[CRITICAL email] RESEND_API_KEY not configured", file=sys.stderr)
        return False

    from_addr = RESEND_FROM_EMAIL or _DEFAULT_FROM

    try:
        resp = requests.post(
            _RESEND_URL,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_addr,
                "to": [to],
                "subject": subject,
                "html": html_body,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        print(
            f"[CRITICAL email] Resend request to {to} failed: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return False

    if resp.status_code >= 400:
        # Resend returns JSON like {"name":"validation_error","message":"..."}.
        body = resp.text[:300].replace("\n", " ")
        print(
            f"[CRITICAL email] Resend rejected send to {to} "
            f"(HTTP {resp.status_code}): {body}",
            file=sys.stderr,
        )
        return False

    return True


def password_reset_html(action_link: str) -> str:
    """HireDrop password reset HTML email."""
    return f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f5f3ff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <div style="max-width:480px;margin:40px auto;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e9e6f5;">
    <div style="padding:32px;">
      <h1 style="font-size:20px;color:#1a1a2e;margin:0 0 8px;">Reset your password</h1>
      <p style="font-size:14px;color:#6b6b8a;line-height:1.6;margin:0 0 24px;">
        We received a request to reset your HireDrop password. Click the button
        below to choose a new one. This link expires in 1 hour. If you didn't
        request this, you can safely ignore this email.
      </p>
      <a href="{action_link}"
         style="display:inline-block;background:linear-gradient(135deg,#6c5ce7,#a78bfa);color:#ffffff;
                text-decoration:none;font-size:14px;font-weight:700;padding:12px 28px;border-radius:8px;">
        Reset password &rarr;
      </a>
      <p style="font-size:12px;color:#9b9bb0;line-height:1.6;margin:24px 0 0;">
        If the button doesn't work, paste this link into your browser:<br>
        <span style="color:#6c5ce7;word-break:break-all;">{action_link}</span>
      </p>
    </div>
  </div>
</body>
</html>
"""
