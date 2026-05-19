"""Send transactional/outreach emails via the user's own Gmail via OAuth.

Architecture (LOCKED 2026-05-18, see ~/.claude/plans/swift-squishing-owl.md):
the user grants us `gmail.send` scope; we store their refresh token and send
on their behalf so the recipient sees `From: User Name <user@gmail.com>`.

This file is the low-level send primitive. Routers compose it with
templates + per-recipient rendering + credit charging.
"""

import base64
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from config import GOOGLE_GMAIL_CLIENT_ID, GOOGLE_GMAIL_CLIENT_SECRET

GMAIL_TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_API_VERSION = "v1"


def _build_gmail_client(refresh_token: str):
    """Build an authorized Gmail API client from the user's refresh token.

    Returns None if creds can't be refreshed (revoked, etc.). Caller should
    treat that as the user needing to reconnect Gmail.
    """
    if not GOOGLE_GMAIL_CLIENT_ID or not GOOGLE_GMAIL_CLIENT_SECRET:
        print("[CRITICAL gmail] GOOGLE_GMAIL_CLIENT_ID/SECRET not configured", file=sys.stderr)
        return None

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=GMAIL_TOKEN_URI,
        client_id=GOOGLE_GMAIL_CLIENT_ID,
        client_secret=GOOGLE_GMAIL_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )

    try:
        creds.refresh(Request())
    except Exception as e:
        print(f"[CRITICAL gmail] Refresh token rejected: {type(e).__name__}: {e}", file=sys.stderr)
        return None

    return build("gmail", GMAIL_API_VERSION, credentials=creds, cache_discovery=False)


def send_via_gmail(
    refresh_token: str,
    sender_email: str,
    sender_name: str | None,
    to_email: str,
    subject: str,
    html_body: str,
) -> dict:
    """Send one HTML email on behalf of the user.

    Returns {"ok": True, "message_id": "..."} on success, or
    {"ok": False, "error": "..."} on failure. All failure paths log to stderr
    with `[CRITICAL gmail]` prefix.
    """
    client = _build_gmail_client(refresh_token)
    if client is None:
        return {"ok": False, "error": "gmail_auth_failed"}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender_email}>" if sender_name else sender_email
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    try:
        sent = client.users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as e:
        print(f"[CRITICAL gmail] send to {to_email} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return {"ok": False, "error": str(e)[:200]}

    return {"ok": True, "message_id": sent.get("id", "")}
