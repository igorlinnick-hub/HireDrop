"""Supabase client hardening — the stale-keepalive 500s (ops-watch catch, 2026-09-06)."""

import os
from unittest.mock import patch

import httpx


def test_postgrest_session_is_hardened():
    """get_supabase() must swap postgrest's session for one with a short keepalive
    expiry and connect retries — the default long-lived socket served
    `httpx.ReadError [Errno 11]` 500s in prod."""
    with (
        patch.dict(
            os.environ,
            {"SUPABASE_URL": "https://test.supabase.co", "SUPABASE_SERVICE_KEY": "k"},
        ),
        patch("app.db.client._client", None),
    ):
        from app.db.client import get_supabase

        client = get_supabase()
        s = client.postgrest.session
        assert isinstance(s, httpx.Client)
        pool = s._transport._pool
        assert pool._keepalive_expiry == 15.0
        assert s._transport._pool._retries == 1
