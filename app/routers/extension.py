import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.db import extension_keys as ext_keys_db
from app.db import selectors as selectors_db
from app.db.subscriptions import is_admin
from app.deps import get_current_user
from config import SENTRY_DSN

router = APIRouter(tags=["extension"])


@router.post("/extension/issue-key")
def issue_extension_key(user=Depends(get_current_user)):
    """Mint a durable extension API key for the authenticated user (Approach A).

    Called by the dashboard connect flow (authenticated with the user's Supabase JWT).
    Returns the RAW key ONCE — the dashboard hands it to the extension, which then uses it
    for all backend calls instead of the short-lived, dashboard-pushed access token.
    Re-issuing revokes any prior key. This endpoint requires a valid JWT, so a leaked
    extension key can't be used to mint another (no privilege escalation).
    """
    raw = ext_keys_db.issue(user.id, getattr(user, "email", None))
    return {"key": raw}


@router.get("/extension/selectors/{platform}")
def get_selectors(platform: str, user=Depends(get_current_user)):
    row = selectors_db.get(platform)
    if not row:
        return JSONResponse(
            status_code=404, content={"error": f"No selectors for platform: {platform}"}
        )
    return {
        "platform": row["platform"],
        "version": row["version"],
        "selectors": row["selectors_json"],
        "updated_at": row["updated_at"],
    }


@router.patch("/extension/selectors/{platform}/{section}")
def patch_selectors_section(
    platform: str, section: str, payload: dict, user=Depends(get_current_user)
):
    """Admin-only: patch any top-level section of platform selectors_json."""
    if not is_admin(getattr(user, "email", None)):
        return JSONResponse(status_code=403, content={"error": "Admin only"})
    row = selectors_db.get(platform)
    if not row:
        return JSONResponse(
            status_code=404, content={"error": f"No selectors for platform: {platform}"}
        )
    selectors_json = row["selectors_json"]
    selectors_json[section] = payload
    selectors_db.upsert(platform, row["version"], selectors_json)
    return {"ok": True, "platform": platform, "section": section}


# Error relay limits. Per user, per uvicorn worker (Procfile runs 2): in-memory state, so
# the real ceiling is twice this — enough to see a bug, not enough for one install stuck
# in a loop to spend the month's Sentry quota (5k errors on the free plan).
_ERROR_WINDOW_SEC = 3600
_ERROR_MAX_PER_USER = 20
_error_reports: dict[str, list[float]] = {}


def _error_rate_limited(user_id: str) -> bool:
    now = time.time()
    if len(_error_reports) > 5000:  # bounded memory; a reset only forgives one window
        _error_reports.clear()
    recent = [t for t in _error_reports.get(user_id, []) if now - t < _ERROR_WINDOW_SEC]
    if len(recent) >= _ERROR_MAX_PER_USER:
        _error_reports[user_id] = recent
        return True
    recent.append(now)
    _error_reports[user_id] = recent
    return False


class ExtensionErrorContext(BaseModel):
    platform: str | None = Field(None, max_length=40)
    phase: str | None = Field(None, max_length=40)
    url_host: str | None = Field(None, max_length=253)


class ExtensionErrorReport(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)
    # Accepted up to 16k so a long stack is never the reason a report is lost; only the
    # first 4k chars are forwarded (app/observability.py::relay_extension_error).
    stack: str | None = Field(None, max_length=16_000)
    ext_version: str | None = Field(None, max_length=32)
    context: ExtensionErrorContext = Field(default_factory=ExtensionErrorContext)


@router.post("/extension/error", status_code=204)
def report_extension_error(body: ExtensionErrorReport, user=Depends(get_current_user)):
    """Relay one of the extension's own errors to Sentry, through us.

    Why a relay and not a Sentry SDK in the extension: sending straight to Sentry needs a
    new host permission, and a permission-warning update disables the extension for every
    installed user until they re-accept. We already hold the permission for this host.

    Contract (for the extension side):
        POST {API_BASE}/api/v1/extension/error
        Authorization: Bearer <the same credential as /extension/ping — the hd_… key>
        Content-Type: application/json
        {
          "message": "TypeError: x is undefined",     required, 1..1000 chars
          "stack": "TypeError: …\\n    at fill (content.js:12:3)",  optional, <=16000 chars
          "ext_version": "1.8.43",                     optional, chrome.runtime.getManifest().version
          "context": {                                 optional, each field optional
            "platform": "indeed",                      <=40 chars
            "phase": "fill",                           <=40 chars
            "url_host": "www.indeed.com"               <=253 chars, HOST ONLY — never location.href
          }
        }
        → 204  accepted (or dropped: SENTRY_DSN unset on the server — nothing is sent anywhere)
        → 401  invalid / revoked credential (no Authorization header at all → 422)
        → 422  a field over its limit or the wrong type — trim before sending
        → 429  over the hourly cap (20 per user per server worker) — drop it, do not retry

    Send Error.message and Error.stack only — never field values, page text or profile
    data. The server cuts URLs to their host and blanks emails, phones and tokens anyway,
    but a name or an answer inside a message has no pattern to catch it.
    """
    if not SENTRY_DSN:
        return Response(status_code=204)
    if _error_rate_limited(str(user.id)):
        return JSONResponse(status_code=429, content={"error": "Too many error reports"})
    from app.observability import relay_extension_error

    relay_extension_error(
        user_id=user.id,
        message=body.message,
        stack=body.stack,
        ext_version=body.ext_version,
        platform=body.context.platform,
        phase=body.context.phase,
        url_host=body.context.url_host,
    )
    return Response(status_code=204)
