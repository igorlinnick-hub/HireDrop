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


class FakeUser:
    id = "00000000-0000-0000-0000-000000000001"
    email = "test@example.com"


@pytest.fixture
def fake_user():
    return FakeUser()


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
