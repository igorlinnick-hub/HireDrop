"""Sentry scrubbing (app/observability.py): what a job seeker typed must never reach Sentry.

Two layers are tested. The pure scrubbers get hand-built events. The real SDK, started with
the production options and a transport that keeps envelopes in memory, gets a FastAPI
request carrying a resume, a phone and employer answers — and the bytes that would have
gone over the wire are searched for every one of them.
"""

import logging
from urllib.parse import quote

import pytest
import sentry_sdk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from app.observability import (
    host_only,
    init_sentry,
    relay_extension_error,
    scrub_breadcrumb,
    scrub_event,
    scrub_text,
    scrub_url,
)

# Kept far from any code that raises: Sentry ships ~5 source lines around each frame, and
# a literal next to a `raise` would show up as source context, not as leaked data.
EMAIL = "jane.roe@example.com"
PHONE = "(808) 555-0142"
RESUME = "Jane Roe - 12 Ocean View Ave, Honolulu HI 96815 - Senior ICU Nurse"
ANSWER = "Yes, I will require H-1B visa sponsorship"
SALARY = "USD 187,654 per year"
RESET_TOKEN = "s3cr3t-reset-token-Zq9"
CLIENT_IP = "203.0.113.77"
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1MSJ9.c2lnbmF0dXJl"  # pragma: allowlist secret
EXT_KEY = "hd_" + "Kx7pQ2mR9vT4wZ8yB3nC6"  # pragma: allowlist secret

SECRETS = [
    EMAIL,
    "jane.roe",
    "555-0142",
    "Ocean View",
    "ICU Nurse",
    "H-1B",
    "187,654",
    RESET_TOKEN,
    CLIENT_IP,
    JWT,
    EXT_KEY,
]

DSN = "https://public@o0.ingest.sentry.io/0"


class _Capture(Transport):
    def __init__(self):
        super().__init__()
        self.envelopes = []

    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)


@pytest.fixture
def sentry_wire(monkeypatch):
    """The production init with an in-memory transport. Yields the transport; the SDK is
    reset afterwards so no other test runs with Sentry on."""
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    transport = _Capture()
    init_sentry(DSN, transport=transport)
    sentry_sdk.get_isolation_scope().clear_breadcrumbs()
    yield transport
    sentry_sdk.get_isolation_scope().clear_breadcrumbs()
    sentry_sdk.get_global_scope().set_client(None)


def _wire(transport) -> str:
    return b"".join(e.serialize() for e in transport.envelopes).decode("utf-8", "replace")


def _events(transport) -> list[dict]:
    return [e.get_event() for e in transport.envelopes if e.get_event()]


def _assert_clean(transport):
    wire = _wire(transport)
    assert wire, "nothing was captured — the test proves nothing"
    leaked = [s for s in SECRETS if s in wire]
    assert not leaked, f"leaked to Sentry: {leaked}"


# --- the SDK end to end -------------------------------------------------------------


def _profile_app() -> FastAPI:
    app = FastAPI()

    @app.post("/api/v1/profile")
    async def save_profile(body: dict):
        resume = body["resume"]  # a local the SDK must not ship
        answers = body["answers"]
        raise ValueError(
            f"profile save failed for {body['email']} / {body['phone']} {len(resume + answers[0])}"
        )

    return app


def test_fastapi_request_with_pii_leaves_nothing_behind(sentry_wire):
    client = TestClient(_profile_app(), raise_server_exceptions=False)
    res = client.post(
        f"/api/v1/profile?email={quote(EMAIL)}&token={RESET_TOKEN}",
        json={
            "email": EMAIL,
            "phone": PHONE,
            "resume": RESUME,
            "answers": [ANSWER],
            "salary": SALARY,
        },
        headers={
            "Authorization": f"Bearer {JWT}",
            "Cookie": f"sb-access-token={JWT}",
            "Referer": f"https://hiredrop.io/dashboard?email={quote(EMAIL)}",
            "X-Forwarded-For": CLIENT_IP,
            "User-Agent": "Mozilla/5.0 (test)",
        },
    )
    assert res.status_code == 500

    _assert_clean(sentry_wire)
    [event] = _events(sentry_wire)
    request = event["request"]
    assert request["url"] == "http://testserver/api/v1/profile"
    assert request["method"] == "POST"
    assert {k.lower() for k in request["headers"]} <= {"user-agent", "content-type"}
    assert not {"data", "cookies", "query_string", "env"} & set(request)
    exc = event["exception"]["values"][-1]
    assert exc["type"] == "ValueError"
    assert "[email]" in exc["value"] and "[phone]" in exc["value"]
    assert all("vars" not in f for f in exc["stacktrace"]["frames"])
    # The text pass runs over contexts too and must not touch the SDK's own ids.
    assert len(event["contexts"]["trace"]["trace_id"]) == 32
    assert event["contexts"]["trace"]["trace_id"].isalnum()


def test_logging_error_is_an_event_and_its_args_are_scrubbed(sentry_wire):
    log = logging.getLogger("hiredrop.test_observability")
    log.setLevel(logging.INFO)
    try:
        # An INFO line becomes a breadcrumb on the next event — httpx logs every Supabase
        # call like this, query string included.
        log.info(
            'HTTP Request: GET %s "HTTP/1.1 200 OK"',
            f"https://x.supabase.co/rest/v1/profiles?select=*&email=eq.{quote(EMAIL)}",
        )
        log.error("resume upload failed for %s (%s) from %s", EMAIL, PHONE, CLIENT_IP)
    finally:
        log.setLevel(logging.NOTSET)

    _assert_clean(sentry_wire)
    [event] = _events(sentry_wire)
    assert event["level"] == "error"
    assert "[email] ([phone]) from [ip]" in event["logentry"]["formatted"]
    crumbs = [c["message"] for c in event["breadcrumbs"]["values"]]
    assert any("rest/v1/profiles?[Filtered]" in c for c in crumbs)


def test_logger_exception_drops_locals(sentry_wire):
    log = logging.getLogger("hiredrop.test_observability")
    payload = {"resume": RESUME, "answer": ANSWER, "salary": SALARY}
    try:
        _ = payload["missing"]
    except KeyError:
        log.exception("tailor failed")

    _assert_clean(sentry_wire)
    [event] = _events(sentry_wire)
    frames = event["exception"]["values"][-1]["stacktrace"]["frames"]
    assert frames and all("vars" not in f for f in frames)


def test_http_breadcrumb_query_is_dropped(sentry_wire):
    sentry_sdk.add_breadcrumb(
        category="httplib",
        type="http",
        data={
            "url": f"https://x.supabase.co/rest/v1/profiles?email=eq.{quote(EMAIL)}",
            "http.query": f"email=eq.{quote(EMAIL)}&phone=eq.8085550142",
            "http.method": "GET",
            "status_code": 200,
        },
    )
    sentry_sdk.capture_message("probe")

    _assert_clean(sentry_wire)
    [event] = _events(sentry_wire)
    [crumb] = event["breadcrumbs"]["values"]
    assert crumb["data"]["url"] == "https://x.supabase.co/rest/v1/profiles"
    assert "http.query" not in crumb["data"]
    assert "8085550142" not in _wire(sentry_wire)


def test_init_options_close_the_default_leaks(monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc1234")
    try:
        init_sentry(DSN, transport=_Capture())
        opts = sentry_sdk.get_client().options
        assert opts["release"] == "abc1234"
        assert opts["send_default_pii"] is False
        assert opts["include_local_variables"] is False
        assert opts["max_request_body_size"] == "never"
        assert opts["traces_sample_rate"] == 0
        assert opts["before_send"] is scrub_event
        assert opts["before_breadcrumb"] is scrub_breadcrumb
        # data_collection would switch the SDK to permissive defaults and kill event_scrubber.
        assert "data_collection" not in opts.get("_experiments", {})
        assert opts["event_scrubber"] is not None
    finally:
        sentry_sdk.get_global_scope().set_client(None)


# --- the extension relay ------------------------------------------------------------


def test_extension_error_is_forwarded_tagged_and_host_only(sentry_wire):
    stack = "TypeError: x is undefined\n" + "\n".join(
        f"    at step{i} (chrome-extension://bjideoimenmpcpnhppneehmjplkgkede/content.js:{i}:7)"
        for i in range(400)
    )
    relay_extension_error(
        user_id="user-1",
        message=f"fill failed on https://www.indeed.com/applystart/jane-roe?email={quote(EMAIL)} for {EMAIL}",
        stack=stack,
        ext_version="1.8.43",
        platform="indeed",
        phase="fill",
        url_host=f"https://smartapply.indeed.com/beta/indeedapply/form?token={RESET_TOKEN}",
    )

    _assert_clean(sentry_wire)
    [event] = _events(sentry_wire)
    assert event["level"] == "error"
    assert event["message"] == "fill failed on https://www.indeed.com for [email]"
    assert event["tags"] == {
        "source": "extension",
        "ext_version": "1.8.43",
        "platform": "indeed",
        "phase": "fill",
    }
    assert event["fingerprint"] == [
        "extension",
        "at step0 (content.js)",
        "fill failed on https://www.indeed.com for [email]",
    ]
    ctx = event["contexts"]["extension"]
    assert ctx["url_host"] == "smartapply.indeed.com"
    assert len(ctx["stack"]) <= 4000
    assert ctx["stack"].endswith(":7)")  # cut on a line boundary, not mid-frame
    assert event["user"] == {"id": "user-1"}
    assert "jane-roe" not in _wire(sentry_wire)


# --- the pure scrubbers -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (f"no profile for {EMAIL}", "no profile for [email]"),
        ("owner jane%40x.com", "owner [email]"),
        ("call 808-555-0142 or +1 (808) 555 0199", "call [phone] or [phone]"),
        ("intl +44 20 7946 0958", "intl [phone]"),
        ("phone=8085550142", "phone=[phone]"),
        ("from 203.0.113.77", "from [ip]"),
        (f"Authorization: Bearer {JWT}", "Authorization: Bearer [token]"),
        (f"key {EXT_KEY} revoked", "key [token] revoked"),
        (f"jwt {JWT}", "jwt [token]"),
        (
            f"GET https://hiredrop.io/signup?email={quote(EMAIL)}&affiliate=x#top done",
            "GET https://hiredrop.io/signup?[Filtered] done",
        ),
        (
            "{'code': '23505', 'details': 'Key (email)=(jane@x.com) already exists.'}",
            "{'code': '23505', 'details': 'Key (email)=([Filtered]) already exists.'}",
        ),
        (
            "'details': 'Failing row contains (u1, Jane, Roe, O'Brien St, 96815).'",
            "'details': 'Failing row contains ([Filtered]).'",
        ),
        (
            f"1 validation error\n  [type=string_type, input_value='{RESUME}', input_type=str]",
            "1 validation error\n  [type=string_type, input_value=[Filtered], input_type=str]",
        ),
    ],
)
def test_scrub_text_blanks_pii(raw, want):
    assert scrub_text(raw) == want


@pytest.mark.parametrize(
    "safe",
    [
        "user 3f2a9c1e-7b4d-4e8a-9f10-2b6c8d4e1a77 not found",
        "event 4f3c8a1234567890bcd1234567890ef1",
        "at 2026-10-07T12:34:56.123456+00:00",
        "ext 1.8.43 on python 3.12.4",
        "at fill (chrome-extension://bjideoimenmpcpnhppneehmjplkgkede/content.js:1234:56)",
        "took 1534ms, 7 rows, job 98765",
    ],
)
def test_scrub_text_leaves_ids_times_and_versions(safe):
    assert scrub_text(safe) == safe


def test_scrub_url_drops_credentials_query_and_fragment():
    assert scrub_url("https://u:p@api.hiredrop.io/api/v1/jobs?since=x&email=y#f") == (
        "https://api.hiredrop.io/api/v1/jobs"
    )


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("www.indeed.com", "www.indeed.com"),
        ("WWW.Indeed.com", "www.indeed.com"),
        ("https://boards.greenhouse.io/acme/jobs/1?gh_src=x", "boards.greenhouse.io"),
        ("boards.greenhouse.io/acme/jobs/1", "boards.greenhouse.io"),
        ("localhost:3000", "localhost"),
        ("jane roe", None),
        ("http://[broken", None),
        ("", None),
        (None, None),
    ],
)
def test_host_only(raw, want):
    assert host_only(raw) == want


def test_scrub_event_keeps_an_opaque_user_id_only():
    event = {"user": {"id": 42, "email": EMAIL, "ip_address": CLIENT_IP, "username": "jane"}}
    assert scrub_event(event) == {"user": {"id": "42"}}
    assert scrub_event({"user": {"email": EMAIL}}) == {}


def test_scrub_event_request_allowlist():
    event = {
        "request": {
            "url": f"https://api.hiredrop.io/api/v1/profile?email={EMAIL}#x",
            "method": "PATCH",
            "query_string": f"email={EMAIL}",
            "data": {"resume": RESUME, "phone": PHONE},
            "cookies": {"sb": JWT},
            "env": {"REMOTE_ADDR": CLIENT_IP},
            "headers": {
                "User-Agent": "ua",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {JWT}",
                "Referer": f"https://hiredrop.io/dashboard?email={EMAIL}",
                "X-Forwarded-For": CLIENT_IP,
            },
        }
    }
    assert scrub_event(event)["request"] == {
        "method": "PATCH",
        "url": "https://api.hiredrop.io/api/v1/profile",
        "headers": {"User-Agent": "ua", "Content-Type": "application/json"},
    }


def test_scrub_event_text_fields_and_frames():
    event = {
        "message": f"retry for {EMAIL}",
        "logentry": {"message": "%s", "formatted": PHONE, "params": [PHONE, 3]},
        "extra": {"note": f"ip {CLIENT_IP}", "n": 1},
        "exception": {
            "values": [
                {
                    "type": "APIError",
                    "value": f"Key (phone)=({PHONE}) already exists",
                    "stacktrace": {"frames": [{"function": "save", "vars": {"resume": RESUME}}]},
                }
            ]
        },
        "threads": {"values": [{"stacktrace": {"frames": [{"vars": {"a": ANSWER}}]}}]},
        "contexts": {
            "trace": {
                "trace_id": "4f3c8a1234567890bcd1234567890ef1",
                "span_id": "a1b2c3d4e5f60718",
            },
            "profile": {"phone": PHONE, "owner": EMAIL},
        },
    }
    out = scrub_event(event)
    assert out["message"] == "retry for [email]"
    assert out["logentry"] == {"message": "%s", "formatted": "[phone]", "params": ["[phone]", 3]}
    assert out["extra"] == {"note": "ip [ip]", "n": 1}
    assert out["exception"]["values"][0]["value"] == "Key (phone)=([Filtered]) already exists"
    assert out["exception"]["values"][0]["stacktrace"]["frames"] == [{"function": "save"}]
    assert out["threads"]["values"][0]["stacktrace"]["frames"] == [{}]
    assert out["contexts"] == {
        "trace": {"trace_id": "4f3c8a1234567890bcd1234567890ef1", "span_id": "a1b2c3d4e5f60718"},
        "profile": {"phone": "[phone]", "owner": "[email]"},
    }


class _Exploding(dict):
    def pop(self, *args):
        raise RuntimeError("boom")


def test_unscrubbable_breadcrumb_is_dropped_not_kept_raw():
    # The SDK keeps the ORIGINAL crumb when before_breadcrumb raises — so we must not raise.
    assert scrub_breadcrumb({"message": EMAIL, "data": _Exploding(url="x")}) is None
