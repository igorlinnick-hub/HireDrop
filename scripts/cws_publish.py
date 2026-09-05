#!/usr/bin/env python3
"""Publish the HireDrop extension to the Chrome Web Store from the command line.

Why this exists: every release so far went through the Developer Dashboard by hand —
find the zip in a file picker, upload, click through tabs, submit. That is a dozen
clicks that cannot be reviewed, repeated, or done by a session that isn't Igor.
The store has an official API for exactly this; this script is the whole of it we need.

Stdlib only (urllib) — no pip install, nothing to keep in sync with requirements.txt.

    python scripts/cws_publish.py auth          # one-time: turn a consent code into a refresh token
    python scripts/cws_publish.py status        # what the store thinks the current state is
    python scripts/cws_publish.py upload <zip>  # push a new package to the DRAFT
    python scripts/cws_publish.py publish       # submit the draft for review
    python scripts/cws_publish.py ship <zip>    # upload + publish, the normal release

Credentials live in the environment (or jobflow/.env):
    CWS_CLIENT_ID, CWS_CLIENT_SECRET   — from a Google Cloud "Desktop app" OAuth client
    CWS_REFRESH_TOKEN                  — produced by `auth`, long-lived
    CWS_ITEM_ID                        — the extension's store id (defaults to HireDrop's)

Setup is documented in scripts/CWS_SETUP.md — it is a one-time, 5-minute job.

KNOWN LIMIT: the API publishes a draft and chooses default vs trustedTesters. It does
NOT expose listing *visibility* (public vs unlisted) or the store-listing copy — those
stay in the dashboard. So this automates the release, not the storefront.
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_ITEM_ID = "bjideoimenmpcpnhppneehmjplkgkede"

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
SCOPE = "https://www.googleapis.com/auth/chromewebstore"
# Google's out-of-band flow for desktop clients: the consent screen shows the code
# instead of redirecting, so no local web server is needed just to catch a callback.
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"

API = "https://www.googleapis.com/chromewebstore/v1.1/items"
UPLOAD_API = "https://www.googleapis.com/upload/chromewebstore/v1.1/items"


def env(key: str, default: str = "") -> str:
    """Read a key from the process env, falling back to jobflow/.env.

    The .env parse is deliberately dumb (KEY=VALUE, no quoting rules): these are
    four opaque credentials, not a config language.
    """
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
    """Refuse anything that isn't https.

    Every URL in this file is a hardcoded Google endpoint, but `urlopen` will happily
    open file:// or a custom scheme if one ever reaches it through a config value —
    and this process holds a credential that must never be handed to a local path.
    """
    if not url.startswith("https://"):
        die(f"refusing non-https URL: {url[:60]}")
    return url


def post_form(url: str, fields: dict) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(_https(url), data=body, method="POST")  # noqa: S310
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — scheme checked by _https
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        die(f"{url} → HTTP {e.code}: {e.read().decode()[:400]}")


def access_token() -> str:
    """Trade the long-lived refresh token for a one-hour access token."""
    cid, secret, refresh = env("CWS_CLIENT_ID"), env("CWS_CLIENT_SECRET"), env("CWS_REFRESH_TOKEN")
    missing = [
        n
        for n, v in (
            ("CWS_CLIENT_ID", cid),
            ("CWS_CLIENT_SECRET", secret),
            ("CWS_REFRESH_TOKEN", refresh),
        )
        if not v
    ]
    if missing:
        die(f"missing {', '.join(missing)} — run `auth` first (see scripts/CWS_SETUP.md)")
    data = post_form(
        TOKEN_URL,
        {
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        },
    )
    return data["access_token"]


def api(
    method: str, url: str, token: str, body: bytes | None = None, ctype: str | None = None
) -> dict:
    req = urllib.request.Request(_https(url), data=body, method=method)  # noqa: S310
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("x-goog-api-version", "2")
    if ctype:
        req.add_header("Content-Type", ctype)
    if body is None and method in ("POST", "PUT"):
        req.add_header("Content-Length", "0")
    try:
        with urllib.request.urlopen(req, timeout=600) as r:  # noqa: S310 — scheme checked by _https
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        die(f"{method} {url} → HTTP {e.code}: {e.read().decode()[:600]}")


def item_id() -> str:
    return env("CWS_ITEM_ID", DEFAULT_ITEM_ID)


# --- commands ----------------------------------------------------------------------


def cmd_auth():
    """Print the consent URL, then trade the pasted code for a refresh token."""
    cid, secret = env("CWS_CLIENT_ID"), env("CWS_CLIENT_SECRET")
    if not cid or not secret:
        die("set CWS_CLIENT_ID and CWS_CLIENT_SECRET first (scripts/CWS_SETUP.md)")
    params = urllib.parse.urlencode(
        {
            "client_id": cid,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    print("1. Открой эту ссылку, разреши доступ, скопируй код:\n")
    print(f"   {AUTH_URL}?{params}\n")
    code = input("2. Вставь код сюда: ").strip()
    if not code:
        die("код не введён")
    data = post_form(
        TOKEN_URL,
        {
            "client_id": cid,
            "client_secret": secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
    )
    refresh = data.get("refresh_token")
    if not refresh:
        die(f"Google не вернул refresh_token: {data}")
    print("\n✓ Готово. Добавь строку в jobflow/.env:\n")
    print(f"CWS_REFRESH_TOKEN={refresh}\n")


def cmd_status():
    data = api("GET", f"{API}/{item_id()}?projection=DRAFT", access_token())
    state = data.get("uploadState", "?")
    print(f"item        {data.get('id', item_id())}")
    print(f"uploadState {state}")
    if data.get("crxVersion"):
        print(f"crxVersion  {data['crxVersion']}")
    for err in data.get("itemError", []):
        print(f"  ! {err.get('error_code')}: {err.get('error_detail')}")
    return data


def cmd_upload(zip_path: str):
    p = Path(zip_path).expanduser()
    if not p.exists():
        die(f"нет файла: {p}")
    blob = p.read_bytes()
    print(f"→ загружаю {p.name} ({len(blob):,} байт)…")
    data = api(
        "PUT",
        f"{UPLOAD_API}/{item_id()}?uploadType=media",
        access_token(),
        body=blob,
        ctype="application/zip",
    )
    state = data.get("uploadState")
    if state == "FAILURE":
        for err in data.get("itemError", []):
            print(f"  ! {err.get('error_code')}: {err.get('error_detail')}", file=sys.stderr)
        die("загрузка отклонена стором")
    print(f"✓ загружено — uploadState={state}")


def cmd_publish(target: str = "default"):
    print(f"→ отправляю на ревью (publishTarget={target})…")
    data = api("POST", f"{API}/{item_id()}/publish?publishTarget={target}", access_token())
    for s in data.get("status", []):
        print(f"  {s}")
    for d in data.get("statusDetail", []):
        print(f"  {d}")
    print("✓ отправлено. Ревью обычно 1-14 дней; статус — `status`.")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd, rest = args[0], args[1:]
    if cmd == "auth":
        cmd_auth()
    elif cmd == "status":
        cmd_status()
    elif cmd == "upload":
        if not rest:
            die("нужен путь к zip: upload <zip>")
        cmd_upload(rest[0])
    elif cmd == "publish":
        cmd_publish(rest[0] if rest else "default")
    elif cmd == "ship":
        if not rest:
            die("нужен путь к zip: ship <zip>")
        cmd_upload(rest[0])
        cmd_publish(rest[1] if len(rest) > 1 else "default")
    else:
        die(f"неизвестная команда: {cmd}")


if __name__ == "__main__":
    main()
