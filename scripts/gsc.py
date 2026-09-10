#!/usr/bin/env python3
"""Google Search Console from the command line — verify the site, submit the sitemap,
and read what people actually searched to find us.

Why this exists: SEO work is invisible without Search Console. Which queries showed
us, which pages Google indexed, which URLs errored — none of it is knowable from the
repo. Doing that in the web UI means Igor clicking through it and relaying screenshots;
this makes it a command any session can run.

Same shape as scripts/cws_publish.py: stdlib only, one Google OAuth client, a
long-lived refresh token in jobflow/.env. It reuses the CWS *client* (an OAuth client
is not scope-bound) but needs its own token, because the scopes differ.

    python scripts/gsc.py auth        # one-time: Igor clicks "Allow", token is saved
    python scripts/gsc.py token       # the meta tag Google wants on the homepage
    python scripts/gsc.py verify      # claim ownership (needs the tag deployed)
    python scripts/gsc.py add         # add the property to Search Console
    python scripts/gsc.py sitemap     # submit sitemap.xml
    python scripts/gsc.py status      # properties + sitemap state
    python scripts/gsc.py queries [days]   # top search queries (default 28 days)
    python scripts/gsc.py pages [days]     # top landing pages
    python scripts/gsc.py setup       # token → verify → add → sitemap, in order

Credentials (jobflow/.env):
    CWS_CLIENT_ID, CWS_CLIENT_SECRET  — the existing "Desktop app" OAuth client
    GSC_REFRESH_TOKEN                 — written by `auth`

Note on properties: this manages the URL-prefix property https://hiredrop.io/ .
A Domain property (covering every subdomain and http/https) can only be verified by
a DNS TXT record, which needs access to the registrar — meta-tag verification cannot
create one.
"""

import http.server
import json
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SITE = "https://hiredrop.io/"
SITEMAP = "https://hiredrop.io/sitemap.xml"

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
# webmasters = read+write on properties and sitemaps; siteverification = claim ownership.
SCOPES = (
    "https://www.googleapis.com/auth/webmasters https://www.googleapis.com/auth/siteverification"
)

SC_API = "https://www.googleapis.com/webmasters/v3"
SV_API = "https://www.googleapis.com/siteVerification/v1"


def env(key: str, default: str = "") -> str:
    """Read a key from the process env, falling back to jobflow/.env."""
    import os

    if os.getenv(key):
        return os.environ[key]
    dotenv = REPO / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return default


def die(msg: str, code: int = 1):
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(code)


def _https(url: str) -> str:
    """Refuse anything that isn't https — this process holds a Google credential."""
    if not url.startswith("https://"):
        die(f"refusing non-https URL: {url[:60]}")
    return url


def post_form(url: str, fields: dict) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(_https(url), data=body, method="POST")  # noqa: S310
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — scheme checked
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        die(f"{url} → HTTP {e.code}: {e.read().decode()[:400]}")


def api(method: str, url: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_https(url), data=data, method=method)  # noqa: S310
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    elif method in ("POST", "PUT"):
        req.add_header("Content-Length", "0")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 — scheme checked
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        die(f"{method} {url.split('?')[0]} → HTTP {e.code}: {e.read().decode()[:600]}")


def access_token() -> str:
    cid, secret, refresh = env("CWS_CLIENT_ID"), env("CWS_CLIENT_SECRET"), env("GSC_REFRESH_TOKEN")
    if not refresh:
        die("no GSC_REFRESH_TOKEN — run `python scripts/gsc.py auth` first")
    if not cid or not secret:
        die("missing CWS_CLIENT_ID / CWS_CLIENT_SECRET in jobflow/.env")
    return post_form(
        TOKEN_URL,
        {
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        },
    )["access_token"]


def site_path(site: str = SITE) -> str:
    """Search Console addresses properties by URL-encoded siteUrl."""
    return urllib.parse.quote(site, safe="")


# --- auth --------------------------------------------------------------------------


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches Google's redirect so nobody has to copy a code out of a browser."""

    code: str | None = None
    error: str | None = None

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler API
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.code = (params.get("code") or [None])[0]
        _CallbackHandler.error = (params.get("error") or [None])[0]
        ok = _CallbackHandler.code is not None
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Готово — можно закрывать вкладку." if ok else f"Отказано: {_CallbackHandler.error}"
        self.wfile.write(
            f"<html><body style='font:16px system-ui;padding:3rem'>{msg}</body></html>".encode()
        )

    def log_message(self, *args):  # silence the default stderr access log
        pass


def cmd_auth():
    """Print the consent URL, catch the redirect on localhost, save the refresh token."""
    cid, secret = env("CWS_CLIENT_ID"), env("CWS_CLIENT_SECRET")
    if not cid or not secret:
        die("missing CWS_CLIENT_ID / CWS_CLIENT_SECRET in jobflow/.env")

    # Loopback redirect, not the deprecated oob flow. Desktop OAuth clients accept
    # any http://localhost port without registering it.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    redirect = f"http://localhost:{port}/"

    params = urllib.parse.urlencode(
        {
            "client_id": cid,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    print("Открой ссылку и нажми «Разрешить» (аккаунт igor.linnick@gmail.com):\n")
    print(f"  {AUTH_URL}?{params}\n")
    print(f"Жду редиректа на {redirect} (5 минут)…", flush=True)

    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 300
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=300)
    server.server_close()

    if _CallbackHandler.error:
        die(f"доступ не выдан: {_CallbackHandler.error}")
    if not _CallbackHandler.code:
        die("код не пришёл за 5 минут — запусти `auth` ещё раз")

    data = post_form(
        TOKEN_URL,
        {
            "client_id": cid,
            "client_secret": secret,
            "code": _CallbackHandler.code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        },
    )
    refresh = data.get("refresh_token")
    if not refresh:
        die(f"Google не вернул refresh_token: {json.dumps(data)[:300]}")

    dotenv = REPO / ".env"
    text = dotenv.read_text() if dotenv.exists() else ""
    lines = [ln for ln in text.splitlines() if not ln.startswith("GSC_REFRESH_TOKEN=")]
    lines.append(f"GSC_REFRESH_TOKEN={refresh}")
    dotenv.write_text("\n".join(lines) + "\n")
    print("✓ GSC_REFRESH_TOKEN записан в jobflow/.env")


# --- verification + property -------------------------------------------------------


def cmd_token():
    """Ask Google for the meta tag that proves we own the site."""
    body = {"site": {"type": "SITE", "identifier": SITE}, "verificationMethod": "META"}
    data = api("POST", f"{SV_API}/token", access_token(), body)
    print(data["token"])


def cmd_verify():
    """Claim ownership. The meta tag must already be live on the homepage."""
    body = {"site": {"type": "SITE", "identifier": SITE}}
    data = api("POST", f"{SV_API}/webResource?verificationMethod=META", access_token(), body)
    owners = ", ".join(data.get("owners", []))
    print(f"✓ verified {data.get('id', SITE)} — owners: {owners}")


def cmd_add():
    """Add the URL-prefix property to Search Console (idempotent)."""
    api("PUT", f"{SC_API}/sites/{site_path()}", access_token())
    print(f"✓ property added: {SITE}")


def cmd_sitemap(feed: str = SITEMAP):
    api(
        "PUT",
        f"{SC_API}/sites/{site_path()}/sitemaps/{urllib.parse.quote(feed, safe='')}",
        access_token(),
    )
    print(f"✓ sitemap submitted: {feed}")


def cmd_status():
    token = access_token()
    sites = api("GET", f"{SC_API}/sites", token).get("siteEntry", [])
    if not sites:
        print("нет ни одной property")
    for s in sites:
        print(f"  {s['siteUrl']}  ({s.get('permissionLevel')})")
    try:
        feeds = api("GET", f"{SC_API}/sites/{site_path()}/sitemaps", token).get("sitemap", [])
    except SystemExit:
        return
    print("\nsitemaps:")
    for f in feeds:
        contents = f.get("contents", [{}])[0]
        print(
            f"  {f['path']}  submitted={f.get('lastSubmitted', '—')[:10]}  "
            f"downloaded={f.get('lastDownloaded', '—')[:10]}  "
            f"urls={contents.get('submitted', '?')}  indexed={contents.get('indexed', '?')}  "
            f"errors={f.get('errors', 0)} warnings={f.get('warnings', 0)}"
        )


# --- the payoff: what people searched ----------------------------------------------


def _search_analytics(dimension: str, days: int, limit: int = 25) -> list[dict]:
    import datetime

    # Search Console data lags ~2 days; asking up to today just returns fewer rows.
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    body = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "dimensions": [dimension],
        "rowLimit": limit,
    }
    return api(
        "POST", f"{SC_API}/sites/{site_path()}/searchAnalytics/query", access_token(), body
    ).get("rows", [])


def _print_rows(rows: list[dict], label: str):
    if not rows:
        print("пусто — данных ещё нет (после верификации Google собирает их днями)")
        return
    print(f"{label:<52} {'показы':>7} {'клики':>6} {'CTR':>6} {'поз.':>5}")
    for r in rows:
        key = r["keys"][0]
        print(
            f"{key[:52]:<52} {r['impressions']:>7} {r['clicks']:>6} "
            f"{r['ctr'] * 100:>5.1f}% {r['position']:>5.1f}"
        )


def cmd_queries(days: str = "28"):
    _print_rows(_search_analytics("query", int(days)), "запрос")


def cmd_pages(days: str = "28"):
    _print_rows(_search_analytics("page", int(days)), "страница")


def cmd_setup():
    """The whole first-time sequence, in the order Google requires it."""
    print("1/4 verification token…")
    cmd_token()
    print("\n2/4 verify (мета-тег должен быть уже задеплоен)…")
    cmd_verify()
    print("\n3/4 add property…")
    cmd_add()
    print("\n4/4 submit sitemap…")
    cmd_sitemap()
    print("\n✓ готово")
    cmd_status()


COMMANDS = {
    "auth": cmd_auth,
    "token": cmd_token,
    "verify": cmd_verify,
    "add": cmd_add,
    "sitemap": cmd_sitemap,
    "status": cmd_status,
    "queries": cmd_queries,
    "pages": cmd_pages,
    "setup": cmd_setup,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(0 if len(sys.argv) < 2 else 2)
    COMMANDS[sys.argv[1]](*sys.argv[2:])
