"""Durable extension API keys (Approach A — the extension's "agent credential").

The extension holds one opaque, non-rotating key and authenticates every backend call
with it, so it no longer depends on the dashboard pushing a short-lived Supabase access
token. We store only sha256(raw) + a prefix — never the raw key. Owner of the
`extension_keys` table.
"""

import hashlib
import hmac
import secrets

from app.db.client import get_supabase

_PREFIX_LEN = 12  # chars of the raw key used for fast lookup (after the "hd_" tag)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue(user_id: str, email: str | None = None) -> str:
    """Revoke any prior active keys for this user, mint a new one, return the RAW key once."""
    sb = get_supabase()
    # Single active key per user: revoking old ones on re-issue keeps the blast radius small.
    revoke_all(user_id)
    raw = "hd_" + secrets.token_urlsafe(32)
    sb.table("extension_keys").insert({
        "user_id": user_id,
        "email": email,
        "key_hash": _hash(raw),
        "prefix": raw[:_PREFIX_LEN],
    }).execute()
    return raw


def verify(raw: str) -> dict | None:
    """Return {"user_id", "email"} for a valid, non-revoked key, else None. Constant-time compare."""
    if not raw or not raw.startswith("hd_"):
        return None
    sb = get_supabase()
    res = (
        sb.table("extension_keys")
        .select("id,user_id,email,key_hash,revoked_at")
        .eq("prefix", raw[:_PREFIX_LEN])
        .execute()
    )
    want = _hash(raw)
    for row in (res.data or []):
        if row.get("revoked_at"):
            continue
        if hmac.compare_digest(row.get("key_hash", ""), want):
            # best-effort last_used stamp; never block auth on it
            try:
                sb.table("extension_keys").update({"last_used_at": "now()"}).eq("id", row["id"]).execute()
            except Exception:
                pass
            return {"user_id": row["user_id"], "email": row.get("email")}
    return None


def revoke_all(user_id: str) -> None:
    try:
        (
            get_supabase()
            .table("extension_keys")
            .update({"revoked_at": "now()"})
            .eq("user_id", user_id)
            .is_("revoked_at", "null")
            .execute()
        )
    except Exception:
        pass
