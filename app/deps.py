"""Auth dependency — Supabase JWT (dashboard) OR durable extension key (extension)."""

from types import SimpleNamespace

from fastapi import Header, HTTPException

from app.db import extension_keys as ext_keys_db
from app.db.client import get_supabase
from modules import ai_meter


def get_current_user(authorization: str = Header(...)):
    """Resolve the caller from a Bearer credential.

    Plain `def`, not `async def`, on purpose: both paths below are BLOCKING network
    calls (supabase-py is sync). FastAPI runs an async dependency on the event loop,
    so every request's token check froze its whole worker for one Supabase round-trip
    — with 2 uvicorn workers, auth was serialised across all traffic (8 concurrent
    requests with a 0.3 s check: 2.45 s async vs 0.31 s def). A sync dependency runs
    in the threadpool. tests/test_deps_threadpool.py keeps it that way.

    Two accepted credentials, both returning the same user shape (`.id`, `.email`):
    - Durable extension API key ("hd_…", Approach A) — the extension's non-rotating
      credential, verified against extension_keys. Never expires; revocable.
    - Supabase JWT — the dashboard/website session token (default path).
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header format")
    token = authorization.split(" ", 1)[1]

    # Extension durable-key path.
    if token.startswith("hd_"):
        info = ext_keys_db.verify(token)
        if not info:
            raise HTTPException(status_code=401, detail="Invalid or revoked extension key")
        ai_meter.set_user(info["user_id"])
        return SimpleNamespace(id=info["user_id"], email=info.get("email"))

    # Supabase JWT path.
    try:
        response = get_supabase().auth.get_user(token)
        if not response or not response.user:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        ai_meter.set_user(response.user.id)
        return response.user
    except HTTPException:
        raise
    except Exception as err:
        raise HTTPException(status_code=401, detail="Token verification failed") from err
