"""Pytest fixtures for HireDrop smoke tests.

Strategy: env vars set BEFORE importing app, Supabase client mocked at the
module-level singleton so no real network is hit. Auth dependency overridden
via FastAPI's dependency_overrides for endpoints behind get_current_user.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
# The stall-watch sweep is a background loop against a mocked Supabase — tests drive
# scan() directly instead.
os.environ.setdefault("STALL_WATCH_ENABLED", "false")

from app.db import handbacks as _handbacks_module  # noqa: E402 — after the env above


class FakeUser:
    id = "00000000-0000-0000-0000-000000000001"
    email = "test@example.com"


@pytest.fixture
def fake_user():
    return FakeUser()


# Captured before the autouse patch below replaces it, for the test that checks its query.
_REAL_HANDED_BACK_SINCE = _handbacks_module.companies_handed_back_since
_REAL_REQUEUED_URLS = _handbacks_module.requeued_urls
_REAL_REQUEUED_COMPANIES = _handbacks_module.requeued_companies


@pytest.fixture
def real_companies_handed_back_since():
    return _REAL_HANDED_BACK_SINCE


@pytest.fixture
def real_requeued_urls():
    return _REAL_REQUEUED_URLS


@pytest.fixture
def real_requeued_companies():
    return _REAL_REQUEUED_COMPANIES


@pytest.fixture(autouse=True)
def _no_company_history_network():
    """The auto ATS queue reads application history for the per-company cap
    (modules/fit_queue.py). Many tests call that router directly without supabase_mock,
    and would reach for the network. A test that cares patches it itself — the inner
    patch wins."""
    with (
        patch("app.db.applications.companies_applied_since", return_value=[]),
        patch("app.db.handbacks.companies_handed_back_since", return_value=[]),
        patch("app.db.handbacks.requeued_urls", return_value=[]),
        patch("app.db.handbacks.requeued_companies", return_value=[]),
    ):
        yield


@pytest.fixture(autouse=True)
def ai_meter_rows(monkeypatch):
    """Every Anthropic call records itself (modules/ai_meter.py) on a thread that writes to
    Supabase. Tests capture the rows here instead: no thread, no network, and a test that
    cares about what was recorded reads this list. Importing a measurement script switches
    the meter off for the whole process (AI_METER=off); each test starts with it on."""
    monkeypatch.delenv("AI_METER", raising=False)
    rows: list[dict] = []
    with patch("modules.ai_meter._dispatch", rows.append):
        yield rows


@pytest.fixture(autouse=True)
def _no_handback_network():
    """Same for the open hand-backs the ATS queue leaves alone (jobs._waiting_on_person)."""
    with patch("app.db.handbacks.open_urls", return_value=[]):
        yield


@pytest.fixture(autouse=True)
def _no_background_prejudge():
    """The deck read starts a background judge pass when ATS rows lack a verdict
    (app/routers/jobs.py::_prejudge_in_background). A thread outliving the test's patches
    would read the real profile and pool. A test that cares patches it itself."""
    with patch("app.routers.jobs._prejudge_in_background"):
        yield


@pytest.fixture
def supabase_mock():
    """Replace the Supabase singleton with a MagicMock.

    Tests configure return values per-call via fluent chains, e.g.:
        supabase_mock.table.return_value.select.return_value \
            .eq.return_value.execute.return_value.data = [...]
    """
    fake = MagicMock()
    with (
        patch("app.db.client._client", fake),
        patch("app.db.client.get_supabase", return_value=fake),
    ):
        yield fake


@pytest.fixture
def client(supabase_mock):
    """Unauthenticated TestClient — Supabase is mocked but auth NOT overridden."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture
def auth_client(supabase_mock, fake_user):
    """TestClient with get_current_user overridden to return FakeUser."""
    from fastapi.testclient import TestClient

    from app.deps import get_current_user
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: fake_user
    yield TestClient(app)
    app.dependency_overrides.clear()
