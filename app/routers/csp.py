"""CSP violation reports from hiredrop.io.

The site ships its full Content-Security-Policy as Report-Only first
(jobflow-website/next.config.ts): browsers enforce nothing, they POST here
whatever the policy WOULD have blocked. A week of those reports decides whether
the policy can be enforced without breaking Google sign-in, the pixels or the map.

Reports land in the Railway log as one line each — `[csp] directive=… blocked=…
doc=…` — not in a table: the site never holds service_role, and the question
this answers ("is anything still being blocked?") is a grep, not a query.

Public and unauthenticated by nature (the browser sends it, without cookies),
so it is bounded three ways: a body cap, a per-IP window, and a per-violation
memo that logs each distinct violation once an hour with a count instead of
once per page view.
"""

import json
import sys
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response

router = APIRouter(tags=["csp"])

_MAX_BODY = 16 * 1024
_WINDOW_SEC = 3600
_MAX_PER_IP = 120
_attempts: dict[str, list[float]] = {}

# key -> (first logged at, reports swallowed since)
_seen: dict[str, tuple[float, int]] = {}

# Violations caused by the visitor's own browser extensions, not by our page.
_IGNORED_SCHEMES = ("chrome-extension", "moz-extension", "safari-extension", "safari-web-extension")

# A report about some other site's page means someone pointed their policy at us.
_OUR_HOSTS = ("hiredrop.io", "www.hiredrop.io", "localhost", "127.0.0.1")


def _rate_limited(key: str) -> bool:
    now = time.time()
    recent = [t for t in _attempts.get(key, []) if now - t < _WINDOW_SEC]
    if len(recent) >= _MAX_PER_IP:
        _attempts[key] = recent
        return True
    recent.append(now)
    _attempts[key] = recent
    return False


def _client_ip(request: Request) -> str:
    # Railway terminates TLS upstream, so the socket peer is the proxy.
    forwarded = request.headers.get("x-forwarded-for", "")
    return (
        forwarded.split(",")[0].strip() or (request.client.host if request.client else "")
    ) or "?"


def _our_host(host: str) -> bool:
    return host in _OUR_HOSTS or host.endswith(".vercel.app")


def _origin(uri: str) -> str:
    """Where the blocked thing came from, without path or query: signed storage
    URLs and reset links carry tokens, and the origin is all the policy needs.
    Keywords ("inline", "eval", "data", "blob") pass through as they are."""
    parts = urlsplit(uri)
    if parts.scheme in ("http", "https", "ws", "wss") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return (parts.scheme or uri)[:40]


def _violations(payload) -> list[dict]:
    """Both wire formats as plain dicts with camelCase keys: report-uri sends
    {"csp-report": {...hyphenated...}}, the Reporting API sends a list of
    {"type": "csp-violation", "body": {...camelCase...}}."""
    if isinstance(payload, dict) and isinstance(payload.get("csp-report"), dict):
        r = payload["csp-report"]
        return [
            {
                "effectiveDirective": r.get("effective-directive") or r.get("violated-directive"),
                "blockedURL": r.get("blocked-uri"),
                "documentURL": r.get("document-uri"),
                "disposition": r.get("disposition"),
            }
        ]
    if isinstance(payload, list):
        return [
            item["body"]
            for item in payload[:20]
            if isinstance(item, dict)
            and item.get("type") == "csp-violation"
            and isinstance(item.get("body"), dict)
        ]
    return []


def _log_once(directive: str, blocked: str, path: str) -> None:
    key = f"{directive} {blocked} {path}"
    now = time.time()
    first, swallowed = _seen.get(key, (0.0, 0))
    if now - first < _WINDOW_SEC:
        _seen[key] = (first, swallowed + 1)
        return
    if len(_seen) > 2000:
        _seen.clear()
    _seen[key] = (now, 0)
    more = f" (+{swallowed} in the last hour)" if swallowed else ""
    print(f"[csp] directive={directive} blocked={blocked} doc={path}{more}", file=sys.stderr)


@router.post("/csp-report", status_code=204)
async def csp_report(request: Request) -> Response:
    """Always 204: the browser ignores the answer, and a reporter that could
    tell "rejected" from "accepted" would only be probing us."""
    if _rate_limited(f"ip:{_client_ip(request)}"):
        return Response(status_code=204)
    body = await request.body()
    if len(body) > _MAX_BODY:
        return Response(status_code=204)
    try:
        payload = json.loads(body)
    except ValueError:
        return Response(status_code=204)

    for v in _violations(payload):
        doc = urlsplit(str(v.get("documentURL") or ""))
        if not _our_host(doc.hostname or ""):
            continue
        blocked = _origin(str(v.get("blockedURL") or "inline"))
        if blocked in _IGNORED_SCHEMES:
            continue
        directive = str(v.get("effectiveDirective") or "?")[:40]
        _log_once(directive, blocked, (doc.path or "/")[:120])
    return Response(status_code=204)
