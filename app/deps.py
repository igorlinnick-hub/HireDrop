"""Auth dependency — Supabase JWT (dashboard) OR durable extension key (extension)."""

from types import SimpleNamespace

from fastapi import Header, HTTPException

from app.db import extension_keys as ext_keys_db
from app.db.client import get_supabase


async def get_current_user(authorization: str = Header(...)):
    """Resolve the caller from a Bearer credential.

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
        return SimpleNamespace(id=info["user_id"], email=info.get("email"))

    # Supabase JWT path.
    try:
        response = get_supabase().auth.get_user(token)
        if not response or not response.user:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return response.user
    except HTTPException:
        raise
    except Exception as err:
        raise HTTPException(status_code=401, detail="Token verification failed") from err
