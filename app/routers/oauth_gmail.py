"""Gmail OAuth flow for cold-email outreach.

User clicks "Connect Gmail" → frontend hits POST /oauth/gmail/start → we
return Google's consent URL → user grants `gmail.send` → Google redirects
to GET /oauth/gmail/callback → we exchange code for refresh token → store
in profiles.gmail_refresh_token + profiles.gmail_email.

The login Google OAuth is configured in Supabase (separate client). This
router is a SECOND, dedicated OAuth client requesting only `gmail.send`.
Separation = principle of least privilege.
"""

import os
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db.client import get_supabase
from app.deps import get_current_user
from config import (
    FRONTEND_URL,
    GOOGLE_GMAIL_CLIENT_ID,
    GOOGLE_GMAIL_CLIENT_SECRET,
    GOOGLE_GMAIL_REDIRECT_URI,
)

router = APIRouter(tags=["oauth-gmail"])

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
_SCOPE = "https://www.googleapis.com/auth/gmail.send"

# In-memory state store (user_id -> nonce). For a single-instance Railway
# deployment this is fine; switch to Redis if we ever scale horizontally.
_state_store: dict[str, str] = {}


@router.post("/oauth/gmail/start")
def gmail_oauth_start(user=Depends(get_current_user)):
    """Return the Google consent URL the user should be redirected to."""
    if not GOOGLE_GMAIL_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GOOGLE_GMAIL_CLIENT_ID not configured")

    state = secrets.token_urlsafe(24)
    _state_store[state] = str(user.id)

    params = {
        "client_id": GOOGLE_GMAIL_CLIENT_ID,
        "redirect_uri": GOOGLE_GMAIL_REDIRECT_URI,
        "response_type": "code",
        "scope": _SCOPE,
        "access_type": "offline",       # required to get a refresh token
        "prompt": "consent",            # force refresh-token issuance even on re-consent
        "include_granted_scopes": "true",
        "state": state,
    }
    from urllib.parse import urlencode
    return {"url": f"{_AUTH_URL}?{urlencode(params)}"}


@router.get("/oauth/gmail/callback")
def gmail_oauth_callback(code: str = Query(...), state: str = Query(...)):
    """Exchange the code for a refresh token and store it on the user's profile.

    This is called by Google's redirect, not by our own frontend. We finish by
    redirecting the user back to /dashboard/outreach/settings with a status
    query param.
    """
    user_id = _state_store.pop(state, None)
    if not user_id:
        return _redirect_back(error="invalid_state")

    if not GOOGLE_GMAIL_CLIENT_ID or not GOOGLE_GMAIL_CLIENT_SECRET:
        return _redirect_back(error="config")

    # Exchange code → tokens
    try:
        token_resp = requests.post(
            _TOKEN_URL,
            data={
                "code": code,
                "client_id": GOOGLE_GMAIL_CLIENT_ID,
                "client_secret": GOOGLE_GMAIL_CLIENT_SECRET,
                "redirect_uri": GOOGLE_GMAIL_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[CRITICAL gmail-oauth] token exchange failed: {e}", file=sys.stderr)
        return _redirect_back(error="token_exchange")

    if token_resp.status_code != 200:
        print(
            f"[CRITICAL gmail-oauth] token endpoint {token_resp.status_code}: "
            f"{token_resp.text[:200]}",
            file=sys.stderr,
        )
        return _redirect_back(error="token_rejected")

    tokens = token_resp.json()
    refresh_token = tokens.get("refresh_token")
    access_token = tokens.get("access_token")
    if not refresh_token:
        # Google omits refresh_token if the user has already granted before
        # without revoking. We force `prompt=consent` to prevent this, but
        # belt-and-suspenders: surface the issue rather than persist a half-state.
        print("[CRITICAL gmail-oauth] no refresh_token in response", file=sys.stderr)
        return _redirect_back(error="no_refresh_token")

    # Get user's gmail address for display
    gmail_email = ""
    try:
        ui = requests.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if ui.status_code == 200:
            gmail_email = (ui.json() or {}).get("email", "")
    except requests.RequestException:
        pass  # display-only, not fatal

    # Persist on the profile
    try:
        get_supabase().table("profiles").update({
            "gmail_refresh_token": refresh_token,
            "gmail_email": gmail_email,
        }).eq("user_id", user_id).execute()
    except Exception as e:
        print(f"[CRITICAL gmail-oauth] failed to persist refresh token: {e}", file=sys.stderr)
        return _redirect_back(error="persist")

    return _redirect_back(ok=True, email=gmail_email)


@router.post("/oauth/gmail/disconnect")
def gmail_oauth_disconnect(user=Depends(get_current_user)):
    """Forget the user's Gmail credentials. (Best-effort revoke not yet wired.)"""
    get_supabase().table("profiles").update({
        "gmail_refresh_token": None,
        "gmail_email": None,
    }).eq("user_id", str(user.id)).execute()
    return {"disconnected": True}


def _redirect_back(ok: bool = False, email: str = "", error: str = "") -> RedirectResponse:
    """After callback, send the user back to the dashboard with a status."""
    from urllib.parse import urlencode
    qs = urlencode({
        k: v for k, v in {
            "gmail": "connected" if ok else "error",
            "email": email,
            "error": error,
        }.items() if v
    })
    return RedirectResponse(
        url=f"{FRONTEND_URL}/dashboard/outreach/settings?{qs}",
        status_code=302,
    )
