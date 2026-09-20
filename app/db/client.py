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


# PostgREST's max-rows. Every response stops here and says nothing about it: a bare
# select, and even .limit(2000), return exactly 1000 rows (measured 09-20 against a
# 1314-row table). A read that outgrows it keeps "working" on a truncated slice.
PAGE = 1000


def fetch_paged(build, limit: int, page: int = PAGE) -> list:
    """Run a read in pages so it survives past the cap above.

    `build(start, end)` must return the query with `.range(start, end)` applied. Give the
    query a deterministic total order (e.g. `.order("date_found", desc=True).order("id")`)
    — ties that reshuffle between pages silently duplicate and drop rows.
    """
    out: list = []
    for start in range(0, limit, page):
        rows = build(start, min(start + page, limit) - 1).execute().data or []
        out.extend(rows)
        if len(rows) < page:
            break
    return out[:limit]


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
