"""Public auth helpers that don't fit the Supabase-client-only frontend flow.

Currently: password reset. Supabase's built-in email is heavily rate-limited
and unreliable, so we generate the recovery link ourselves with the service
key and deliver it over Gmail SMTP.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import requests
from fastapi import APIRouter

from app.schemas import ForgotPasswordRequest
from config import FRONTEND_URL, SUPABASE_SERVICE_KEY, SUPABASE_URL
from modules.email_sender import password_reset_html, send_email

router = APIRouter(tags=["auth"])

# After the user clicks the link, Supabase verifies the token then redirects
# here; /auth/callback exchanges the code for a session and forwards to
# /auth/update-password.
_REDIRECT_TO = f"{FRONTEND_URL}/auth/callback?next=/auth/update-password"


@router.post("/auth/forgot-password")
def forgot_password(req: ForgotPasswordRequest):
    """Generate a Supabase recovery link and email it via Gmail SMTP.

    Always returns {"sent": true} regardless of whether the email exists —
    we don't want this endpoint to be an account-enumeration oracle.
    """
    email = req.email.strip().lower()
    if not email or "@" not in email:
        return {"sent": True}

    try:
        resp = requests.post(
            f"{SUPABASE_URL}/auth/v1/admin/generate_link",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "type": "recovery",
                "email": email,
                "redirect_to": _REDIRECT_TO,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[auth] generate_link request failed: {e}")
        return {"sent": True}

    if resp.status_code != 200:
        # 422/404 here usually just means "no such user" — stay quiet.
        print(f"[auth] generate_link returned {resp.status_code}: {resp.text[:200]}")
        return {"sent": True}

    action_link = resp.json().get("action_link")
    if not action_link:
        print("[auth] generate_link succeeded but no action_link in response")
        return {"sent": True}

    send_email(email, "Reset your JobFlow password", password_reset_html(action_link))
    return {"sent": True}
