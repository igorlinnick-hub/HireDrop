"""Per-user rate limit on cover letter generation.

The quota slot is CLAIMED atomically before the LLM call (claim_today →
claim_ai_use RPC) — the old check-then-generate-then-increment flow let
parallel requests race past the cap during the seconds-long generation.
Soft mode (RATE_LIMIT_ENFORCE=false) counts but never blocks. A failed
generation refunds its slot (release_today).
"""

from unittest.mock import MagicMock, patch

import pytest


def test_under_limit_passes(auth_client):
    claim = MagicMock(return_value=True)
    with (
        patch("app.routers.tools.RATE_LIMIT_ENFORCE", True),
        patch("app.routers.tools.RATE_LIMIT_LETTERS_PER_DAY", 3),
        patch("app.routers.tools.usage_db.claim_today", claim),
        patch("app.routers.tools.generate_cover_letter", return_value="hi"),
        patch("app.routers.tools.get_profile", return_value={"name": "X"}),
    ):
        res = auth_client.post(
            "/api/v1/tools/cover-letter-preview",
            json={"keywords": "python"},
        )
    assert res.status_code == 200
    assert res.json()["letter"] == "hi"
    claim.assert_called_once()
    assert claim.call_args.args[1] == 3  # enforced path claims against the real cap


def test_over_limit_returns_429(auth_client):
    with (
        patch("app.routers.tools.RATE_LIMIT_ENFORCE", True),
        patch("app.routers.tools.RATE_LIMIT_LETTERS_PER_DAY", 50),
        patch("app.routers.tools.usage_db.claim_today", return_value=False),
        patch("app.routers.tools.usage_db.get_today_count", return_value=50),
        patch("app.routers.tools.get_profile", return_value={"name": "X"}),
    ):
        res = auth_client.post(
            "/api/v1/tools/cover-letter-preview",
            json={"keywords": "python"},
        )
    assert res.status_code == 429
    assert "limit" in res.json()["detail"].lower()


def test_soft_mode_counts_but_does_not_block(auth_client):
    claim = MagicMock(return_value=True)
    with (
        patch("app.routers.tools.RATE_LIMIT_ENFORCE", False),
        patch("app.routers.tools.RATE_LIMIT_LETTERS_PER_DAY", 50),
        patch("app.routers.tools.usage_db.claim_today", claim),
        patch("app.routers.tools.generate_cover_letter", return_value="hi"),
        patch("app.routers.tools.get_profile", return_value={"name": "X"}),
    ):
        res = auth_client.post(
            "/api/v1/tools/cover-letter-preview",
            json={"keywords": "python"},
        )
    assert res.status_code == 200
    # Observe mode still records the use — with the no-ceiling limit.
    claim.assert_called_once()
    assert claim.call_args.args[1] > 50


def test_failed_generation_refunds_slot(auth_client):
    release = MagicMock()
    with (
        patch("app.routers.tools.RATE_LIMIT_ENFORCE", True),
        patch("app.routers.tools.RATE_LIMIT_LETTERS_PER_DAY", 3),
        patch("app.routers.tools.usage_db.claim_today", return_value=True),
        patch("app.routers.tools.usage_db.release_today", release),
        patch("app.routers.tools.generate_cover_letter", side_effect=RuntimeError("api down")),
        patch("app.routers.tools.get_profile", return_value={"name": "X"}),
    ):
        with pytest.raises(RuntimeError):
            auth_client.post(
                "/api/v1/tools/cover-letter-preview",
                json={"keywords": "python"},
            )
    release.assert_called_once()  # the claimed slot went back — nothing was spent


def test_claim_today_falls_back_before_migration():
    """Until claim_ai_use is applied, claim_today degrades to the old two-step
    flow instead of blocking the product."""
    from app.db import usage

    rpc = MagicMock()
    rpc.rpc.return_value.execute.side_effect = Exception("function does not exist")
    with (
        patch("app.db.usage.get_supabase", return_value=rpc),
        patch("app.db.usage.get_today_count", return_value=2),
        patch("app.db.usage.increment_today") as inc,
    ):
        assert usage.claim_today("u1", limit=3) is True
        inc.assert_called_once_with("u1")
        assert usage.claim_today("u1", limit=2) is False
