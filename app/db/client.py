"""Supabase client singleton — единственное место где создаётся клиент."""

import contextlib
import os

import httpx
from supabase import Client, create_client

_client: Client | None = None


def _harden_postgrest_session(client: Client) -> None:
    """Replace postgrest's httpx session with one that can't serve a stale socket.

    Prod threw intermittent 500s (`httpx.ReadError [Errno 11]` on GET /jobs and
    /campaign/status, caught by ops-watch 2026-09-06): the default long-lived
    HTTP/2 keepalive connection goes half-dead between requests and the first
    request on it fails. Short keepalive_expiry stops reuse of idle sockets,
    HTTP/1.1 avoids the h2 stream-reset flavor, and transport retries cover the
    reconnect. Same base_url/headers/timeout — behavior is otherwise identical.
    """
    old = client.postgrest.session
    client.postgrest.session = httpx.Client(
        base_url=old.base_url,
        headers=old.headers,
        timeout=old.timeout,
        # limits= on Client is ignored when transport= is given — they belong INSIDE it.
        transport=httpx.HTTPTransport(
            retries=1,
            limits=httpx.Limits(
                max_connections=20, max_keepalive_connections=10, keepalive_expiry=15.0
            ),
        ),
    )
    with contextlib.suppress(Exception):
        old.close()


def get_supabase() -> Client:
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL", "")
        key = os.getenv("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        _client = create_client(url, key)
        _harden_postgrest_session(_client)
    return _client
