"""Sentry, made safe for job-seeker data — the one place that decides what leaves.

`send_default_pii=False` sounds like enough and is not. The SDK still ships JSON request
bodies up to ~10 KB and the local variables of every stack frame, and on our endpoints
those are the profile, employer answers, salary, work authorization, the resume and the
Drop chat. So `init_sentry` switches both off, and `scrub_event` runs on every event as
the last word before it leaves the process:

  * request  — method, path and two harmless headers; no body, cookies or query string;
  * user     — an opaque id at most;
  * text     — exception messages, log lines, breadcrumbs: emails, phone numbers, IPs,
               tokens and URL query strings are blanked wherever they appear.

Fail-closed by construction: if `scrub_event` raises, the SDK drops the event rather than
sending it raw (client.py wraps before_send in capture_internal_exceptions and keeps None).

Nothing here imports sentry_sdk at module level. With SENTRY_DSN unset the app never loads
the SDK (app/main.py), and the scrubbers stay importable for tests.
"""

import os
import re
from urllib.parse import urlsplit, urlunsplit

# Everything else in request.headers goes: authorization and cookies obviously, but also
# referer (the dashboard URL, query string included), origin and x-forwarded-for (the IP).
_HEADERS_KEPT = ("user-agent", "content-type")

# Order matters: a URL's query string goes first, whole, so an email inside it never
# reaches the email pattern half-encoded.
_TEXT_RULES: list[tuple[re.Pattern, str]] = [
    # https://x.supabase.co/rest/v1/profiles?email=eq.jane%40x.com → …/profiles?[Filtered]
    (re.compile(r"""(\b[a-z][a-z0-9+.-]*://[^\s?#'"<>]*)[?#][^\s'"<>]*""", re.I), r"\1?[Filtered]"),
    # Postgres echoes the offending row: 'Key (email)=(jane@x.com) already exists.' and
    # 'Failing row contains (<every column of the profile>).' — supabase-py puts both
    # into the exception message.
    (re.compile(r"(Key \([^)]*\)=)\(.*?\)(?= (?:already|is) )"), r"\1([Filtered])"),
    (re.compile(r"Failing row contains \(.*\)"), "Failing row contains ([Filtered])"),
    # pydantic quotes the rejected value: "[type=string_type, input_value='…', input_type=int]".
    (re.compile(r"input_value=.*?, input_type=", re.S), "input_value=[Filtered], input_type="),
    (re.compile(r"(Bearer\s+)\S+", re.I), r"\1[token]"),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]*"), "[token]"),  # a JWT on its own
    (re.compile(r"\bhd_[\w-]{8,}"), "[token]"),  # extension API key (app/db/extension_keys.py)
    (re.compile(r"[\w.+-]+(?:@|%40)[\w-]+(?:\.[\w-]+)+"), "[email]"),
    # US numbers in any punctuation (808-555-1234, (808) 555 1234, +1.808.555.1234) and
    # international ones only behind a "+". Word boundaries keep it out of hex ids. A bare
    # 10-digit epoch in a message is redacted too — the price of not missing a phone.
    (
        re.compile(
            r"(?<![\w+])(?:(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}"
            r"|\+\d{1,3}[\s.-]?\d{1,4}(?:[\s.-]?\d{2,4}){2,4})(?!\w)"
        ),
        "[phone]",
    ),
    (re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"), "[ip]"),
]

_HTTP_URL = re.compile(r"""\bhttps?://[^\s'"<>()]+""", re.I)


def scrub_text(text: str) -> str:
    """Blank the PII-shaped substrings of free text. Patterns, not understanding: a name
    or an address in prose gets through, which is why bodies and locals never get here."""
    for pattern, replacement in _TEXT_RULES:
        text = pattern.sub(replacement, text)
    return text


def scrub_url(url: str) -> str:
    """scheme://host/path — no credentials, query string or fragment."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.split("?", 1)[0].split("#", 1)[0]
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def host_only(value: str | None) -> str | None:
    """The host of a URL or of a bare 'host/path?query', lowercased, or None when what is
    left doesn't look like a hostname. The extension reports where an error happened, and
    a page URL can carry a job seeker's application token in its path or query."""
    if not value:
        return None
    value = value.strip()
    try:
        host = (
            urlsplit(value).hostname
            if "://" in value
            else re.split(r"[/?#:]", value, maxsplit=1)[0]
        )
    except ValueError:  # e.g. an unclosed "[" — not an IPv6 literal we want anyway
        return None
    host = (host or "").lower()
    return host if re.fullmatch(r"[a-z0-9.-]{1,253}", host) else None


def _origins_only(text: str) -> str:
    """Every http(s) URL in `text` cut down to its origin."""

    def origin(m: re.Match) -> str:
        try:
            parts = urlsplit(m.group())
            return f"{parts.scheme}://{parts.hostname or ''}"
        except ValueError:
            return "[url]"

    return _HTTP_URL.sub(origin, text)


def _scrub_values(value):
    """Recursively scrub every string in a JSON-shaped value; other leaves pass through."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {k: _scrub_values(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub_values(v) for v in value]
    return value


def _scrub_request(request: dict) -> dict:
    headers = request.get("headers")
    kept = (
        {k: v for k, v in headers.items() if k.lower() in _HEADERS_KEPT}
        if isinstance(headers, dict)
        else {}
    )
    out = {"method": request.get("method"), "headers": kept}
    if isinstance(request.get("url"), str):
        out["url"] = scrub_url(request["url"])
    return {k: v for k, v in out.items() if v}


def _frames(event: dict):
    for section in ("exception", "threads"):
        for value in (event.get(section) or {}).get("values") or []:
            yield from ((value or {}).get("stacktrace") or {}).get("frames") or []


def scrub_breadcrumb(crumb: dict, hint=None) -> dict | None:
    """before_breadcrumb. httpx/stdlib crumbs keep the raw query in data['http.query'];
    log crumbs carry the formatted line. A crumb that can't be cleaned is dropped: the
    SDK would otherwise keep the original (scope.add_breadcrumb ignores a raising hook)."""
    try:
        if isinstance(crumb.get("message"), str):
            crumb["message"] = scrub_text(crumb["message"])
        data = crumb.get("data")
        if isinstance(data, dict):
            data.pop("http.query", None)
            data.pop("http.fragment", None)
            if isinstance(data.get("url"), str):
                data["url"] = scrub_url(data["url"])
            crumb["data"] = _scrub_values(data)
        return crumb
    except Exception:
        return None


def scrub_event(event: dict, hint=None) -> dict:
    """before_send. Runs after the SDK's own serialization, so `event` is plain JSON data."""
    if isinstance(event.get("request"), dict):
        event["request"] = _scrub_request(event["request"])

    if "user" in event:
        user = event["user"]
        uid = user.get("id") if isinstance(user, dict) else None
        if uid:
            event["user"] = {"id": str(uid)}
        else:
            del event["user"]

    # include_local_variables=False already keeps these out; this is the belt to that brace.
    for frame in _frames(event):
        if isinstance(frame, dict):
            frame.pop("vars", None)

    for value in (event.get("exception") or {}).get("values") or []:
        if isinstance(value, dict) and isinstance(value.get("value"), str):
            value["value"] = scrub_text(value["value"])

    # capture_message puts a string here; the protocol also allows {message, formatted, params}.
    for key in ("message", "logentry"):
        if key in event:
            event[key] = _scrub_values(event[key])
    # contexts too: SDK-filled ones are ids and versions, but set_context() is one line away.
    for key in ("extra", "tags", "contexts"):
        if isinstance(event.get(key), dict):
            event[key] = _scrub_values(event[key])

    crumbs = event.get("breadcrumbs")
    if isinstance(crumbs, dict) and isinstance(crumbs.get("values"), list):
        crumbs["values"] = [
            c for c in (scrub_breadcrumb(c) for c in crumbs["values"] if isinstance(c, dict)) if c
        ]
    return event


def init_sentry(dsn: str, **overrides) -> None:
    """Start the SDK with nothing left to defaults that could carry PII. `overrides` is for
    tests (a capturing transport); production passes only the DSN."""
    import logging

    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration, ignore_logger

    # uvicorn's access line is "<client ip> - GET /path?query 200": an IP and a query string
    # for every request, as a breadcrumb on whatever event comes next.
    ignore_logger("uvicorn.access")

    options = {
        "dsn": dsn,
        "environment": os.getenv("RAILWAY_ENVIRONMENT_NAME", "prod"),
        # Railway sets this on every deploy, so an issue names the commit that shipped it.
        "release": os.getenv("RAILWAY_GIT_COMMIT_SHA") or None,
        "traces_sample_rate": 0,  # errors only — no perf tracing spend
        "send_default_pii": False,  # job-seeker PII must not leave our infra
        # The two leaks send_default_pii leaves open (see the module docstring).
        "include_local_variables": False,
        "max_request_body_size": "never",
        "before_send": scrub_event,
        "before_breadcrumb": scrub_breadcrumb,
        # NOT `data_collection`: passing it switches the SDK to permissive defaults and turns
        # its own key-name scrubber off.
        "integrations": [
            # logging.error / logger.exception become events, INFO and up become breadcrumbs.
            # print() is invisible to Sentry: the failures app/ and modules/ catch and print
            # stay Railway-log only until they move to logging. Sentry Logs (the separate log
            # product) stays off: it has no before_send of ours in front of it.
            LoggingIntegration(
                level=logging.INFO, event_level=logging.ERROR, capture_sentry_logs=False
            ),
        ],
    }
    options.update(overrides)
    sentry_sdk.init(**options)


def _first_frame(stack: str) -> str:
    """The top 'at fn (file:line:col)' line, without line/col (they move every release) and
    without the extension id (each unpacked install has its own)."""
    for line in stack.splitlines():
        line = line.strip()
        if line.startswith("at "):
            line = re.sub(r"chrome-extension://[a-z]+/", "", line)
            return re.sub(r":\d+:\d+(?=\)?$)", "", line)[:200]
    return ""


def _tag(value: str | None) -> str:
    return re.sub(r"[^\w.-]", "_", value or "")[:40] or "unknown"


def relay_extension_error(
    user_id: str,
    message: str,
    stack: str | None,
    ext_version: str | None,
    platform: str | None,
    phase: str | None,
    url_host: str | None,
    max_stack_chars: int = 4000,
) -> None:
    """Forward one extension-side error to Sentry as an error-level message.

    The extension runs on employers' pages, so its text is treated as hostile to privacy:
    URLs are cut to their origin before the usual scrub, and the stack is cut to the first
    `max_stack_chars` characters on a line boundary. Grouping is by top frame + message, so
    one bug firing on every job card is one issue, not hundreds."""
    import sentry_sdk

    message = scrub_text(_origins_only(message))
    stack = scrub_text(_origins_only(stack or ""))
    if len(stack) > max_stack_chars:
        stack = stack[:max_stack_chars].rsplit("\n", 1)[0]
    sentry_sdk.capture_message(
        message,
        level="error",
        tags={
            "source": "extension",
            "ext_version": _tag(ext_version),
            "platform": _tag(platform),
            "phase": _tag(phase),
        },
        fingerprint=["extension", _first_frame(stack), message[:200]],
        contexts={"extension": {"stack": stack, "url_host": host_only(url_host)}},
        user={"id": str(user_id)},
    )
