"""POST /extension/error — the extension's own errors, relayed to Sentry through us.

Same credential as /extension/ping, strict field limits, a per-user rate limit, and a
complete no-op (204, nothing forwarded) while SENTRY_DSN is unset.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import sentry_sdk
from fastapi.testclient import TestClient

from app.deps import get_current_user
from app.main import app
from app.routers import extension
from tests.test_observability import DSN, EMAIL, _Capture, _events, _wire

URL = "/api/v1/extension/error"
REPORT = {
    "message": "TypeError: Cannot read properties of null (reading 'click')",
    "stack": "TypeError: Cannot read properties of null\n    at clickNext (chrome-extension://abc/content.js:812:14)",
    "ext_version": "1.8.43",
    "context": {"platform": "indeed", "phase": "fill", "url_host": "smartapply.indeed.com"},
}


@pytest.fixture(autouse=True)
def _fresh_limits():
    extension._error_reports.clear()
    yield
    extension._error_reports.clear()


@pytest.fixture
def as_user(supabase_mock):
    """TestClient whose caller can be switched mid-test: as_user.login("u2")."""
    who = {"id": "user-1"}
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=who["id"], email=None)
    client = TestClient(app)
    client.login = lambda uid: who.update(id=uid)
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def relay():
    with (
        patch.object(extension, "SENTRY_DSN", DSN),
        patch("app.observability.relay_extension_error") as fwd,
    ):
        yield fwd


def test_unauthenticated_is_rejected(client):
    with patch("app.observability.relay_extension_error") as fwd:
        assert client.post(URL, json=REPORT).status_code in (401, 422)  # no header at all
        r = client.post(URL, json=REPORT, headers={"Authorization": "NotBearer x"})
        assert r.status_code == 401
        # A revoked/unknown extension key: the same path /extension/ping uses.
        r = client.post(URL, json=REPORT, headers={"Authorization": "Bearer hd_revokedkey123456"})
        assert r.status_code == 401
    fwd.assert_not_called()


def test_without_dsn_it_is_a_no_op(as_user):
    with (
        patch.object(extension, "SENTRY_DSN", ""),
        patch("app.observability.relay_extension_error") as fwd,
    ):
        for _ in range(30):  # past the rate limit too: nothing is counted or sent
            assert as_user.post(URL, json=REPORT).status_code == 204
    fwd.assert_not_called()
    assert extension._error_reports == {}
    assert not sentry_sdk.get_client().is_active()  # app.main never started the SDK


def test_forwards_fields_with_dsn(as_user, relay):
    r = as_user.post(URL, json=REPORT)
    assert r.status_code == 204
    assert r.content == b""
    relay.assert_called_once_with(
        user_id="user-1",
        message=REPORT["message"],
        stack=REPORT["stack"],
        ext_version="1.8.43",
        platform="indeed",
        phase="fill",
        url_host="smartapply.indeed.com",
    )


def test_only_message_is_required(as_user, relay):
    assert as_user.post(URL, json={"message": "boom"}).status_code == 204
    assert relay.call_args.kwargs["stack"] is None
    assert relay.call_args.kwargs["platform"] is None


@pytest.mark.parametrize(
    "bad",
    [
        {"message": ""},
        {"message": "x" * 1001},
        {"message": "boom", "stack": "s" * 16_001},
        {"message": "boom", "ext_version": "1" * 33},
        {"message": "boom", "context": {"platform": "p" * 41}},
        {"message": "boom", "context": {"phase": "p" * 41}},
        {"message": "boom", "context": {"url_host": "h" * 254}},
        {"message": ["not", "a", "string"]},
        {},
    ],
)
def test_size_limits(as_user, relay, bad):
    assert as_user.post(URL, json=bad).status_code == 422
    relay.assert_not_called()


def test_rate_limit_is_per_user(as_user, relay):
    for _ in range(extension._ERROR_MAX_PER_USER):
        assert as_user.post(URL, json=REPORT).status_code == 204
    assert as_user.post(URL, json=REPORT).status_code == 429
    assert relay.call_count == extension._ERROR_MAX_PER_USER

    as_user.login("user-2")  # someone else's broken extension doesn't silence this one
    assert as_user.post(URL, json=REPORT).status_code == 204


def test_window_slides(as_user, relay):
    with patch("app.routers.extension.time.time", return_value=1_000_000.0):
        for _ in range(extension._ERROR_MAX_PER_USER):
            as_user.post(URL, json=REPORT)
        assert as_user.post(URL, json=REPORT).status_code == 429
    later = 1_000_000.0 + extension._ERROR_WINDOW_SEC + 1
    with patch("app.routers.extension.time.time", return_value=later):
        assert as_user.post(URL, json=REPORT).status_code == 204


def test_end_to_end_report_is_scrubbed(as_user, monkeypatch):
    """The real relay and the real SDK: the bytes for Sentry hold no page URL path, query,
    email or raw request body — only the host, tags and the cut stack."""
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    from app.observability import init_sentry

    transport = _Capture()
    init_sentry(DSN, transport=transport)
    try:
        with patch.object(extension, "SENTRY_DSN", DSN):
            r = as_user.post(
                URL,
                json={
                    "message": f"no Next on https://www.indeed.com/apply/jane-roe-resume?jk=abc&e={EMAIL}",
                    "stack": "Error: no Next\n    at step (chrome-extension://abc/content.js:9:1)\n"
                    + "    at loop (chrome-extension://abc/content.js:10:1)\n" * 200,
                    "ext_version": "1.8.43",
                    "context": {
                        "platform": "indeed",
                        "phase": "fill",
                        "url_host": "https://www.indeed.com/apply/jane-roe-resume?jk=abc",
                    },
                },
            )
        assert r.status_code == 204
        wire = _wire(transport)
        for leaked in ("jane-roe", "jk=abc", EMAIL, "jane.roe"):
            assert leaked not in wire
        [event] = _events(transport)
        assert event["message"] == "no Next on https://www.indeed.com"
        assert event["tags"]["source"] == "extension"
        assert event["contexts"]["extension"]["url_host"] == "www.indeed.com"
        assert len(event["contexts"]["extension"]["stack"]) <= 4000
        assert "data" not in event.get("request", {})
    finally:
        sentry_sdk.get_global_scope().set_client(None)
