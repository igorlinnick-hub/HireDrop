"""Free taste (FREE_TASTE_PLAN.md): lifetime 40-app cap on the free tier.

The gate lives in check_can_apply (lifetime check BEFORE the daily cap so the
exhausted user sees the paywall, not "come back tomorrow"), the counter moves
only through the atomic increment RPC on a real application save, and
disposable-email accounts are rejected at campaign start.
"""

from unittest.mock import patch

from app.disposable_email import is_disposable_email

# ---------------------------------------------------------------------------
# check_can_apply — lifetime gate
# ---------------------------------------------------------------------------


def _check(free_used, used_today=0, tier="free"):
    from app.db import subscriptions

    with (
        patch("app.db.subscriptions.get_tier", return_value=tier),
        patch("app.db.subscriptions.get_free_apps_used", return_value=free_used),
        patch("app.db.subscriptions.get_submit_mode", return_value="auto"),
        patch("app.db.subscriptions.apps_db.count_today", return_value=used_today),
        patch("app.db.subscriptions.apps_db.count_today_by_platform", return_value={}),
    ):
        return subscriptions.check_can_apply("u1", "indeed")


def test_free_under_limit_allowed():
    res = _check(free_used=39)
    assert res["allowed"] is True
    assert res["free_used"] == 39
    assert res["free_limit"] == 40


def test_free_at_limit_denied_with_paywall_reason():
    res = _check(free_used=40)
    assert res["allowed"] is False
    assert "subscribe" in res["reason"].lower()
    assert res["free_used"] == 40
    assert res["free_limit"] == 40


def test_lifetime_gate_wins_over_daily_message():
    # Exhausted taste AND exhausted day → the paywall reason must surface,
    # not "Daily limit reached".
    res = _check(free_used=40, used_today=20)
    assert res["allowed"] is False
    assert "free applications" in res["reason"]


def test_paid_tier_skips_lifetime_gate():
    with patch("app.db.subscriptions.get_free_apps_used") as free_read:
        res = _check(free_used=None, tier="pro")
    assert res["allowed"] is True
    assert res["free_used"] is None
    assert res["free_limit"] is None
    free_read.assert_not_called()


# ---------------------------------------------------------------------------
# /applications/save — increment only on a real save, only for free tier
# ---------------------------------------------------------------------------

_SAVE_BODY = {"job_title": "Dev", "company": "Acme", "platform": "indeed"}


def test_save_increments_free_counter(auth_client):
    check = {
        "allowed": True,
        "reason": "",
        "tier": "free",
        "used_today": 3,
        "daily_limit": 20,
        "platform_used": 1,
        "free_used": 12,
        "free_limit": 40,
    }
    with (
        patch("app.routers.applications.check_can_apply", return_value=check),
        patch("app.routers.applications.increment_free_apps", return_value=13) as inc,
        patch("app.routers.applications.jobs_db.save_job", return_value="job-1"),
        patch("app.routers.applications.apps_db.save_application"),
    ):
        res = auth_client.post("/api/v1/applications/save", json=_SAVE_BODY)
    assert res.status_code == 200
    inc.assert_called_once()
    body = res.json()
    assert body["free_used"] == 13
    assert body["free_limit"] == 40


def test_save_does_not_increment_for_paid(auth_client):
    check = {
        "allowed": True,
        "reason": "",
        "tier": "pro",
        "used_today": 3,
        "daily_limit": 30,
        "platform_used": 1,
        "free_used": None,
        "free_limit": None,
    }
    with (
        patch("app.routers.applications.check_can_apply", return_value=check),
        patch("app.routers.applications.increment_free_apps") as inc,
        patch("app.routers.applications.jobs_db.save_job", return_value="job-1"),
        patch("app.routers.applications.apps_db.save_application"),
    ):
        res = auth_client.post("/api/v1/applications/save", json=_SAVE_BODY)
    assert res.status_code == 200
    inc.assert_not_called()


def test_save_denied_no_increment(auth_client):
    check = {
        "allowed": False,
        "reason": "You've used all 40 free applications — subscribe to keep applying.",
        "tier": "free",
        "used_today": 0,
        "daily_limit": 20,
        "platform_used": 0,
        "free_used": 40,
        "free_limit": 40,
    }
    with (
        patch("app.routers.applications.check_can_apply", return_value=check),
        patch("app.routers.applications.increment_free_apps") as inc,
        patch("app.routers.applications.jobs_db.save_job") as save_job,
    ):
        res = auth_client.post("/api/v1/applications/save", json=_SAVE_BODY)
    assert res.status_code == 429
    inc.assert_not_called()
    save_job.assert_not_called()
    body = res.json()
    assert body["free_used"] == 40
    assert body["free_limit"] == 40
    assert "subscribe" in body["message"].lower()


# ---------------------------------------------------------------------------
# Disposable-email guard at campaign start
# ---------------------------------------------------------------------------


def test_disposable_domains_detected():
    assert is_disposable_email("x@mailinator.com")
    assert is_disposable_email("x@MAILINATOR.com")
    assert is_disposable_email("x@mail.mailinator.com")  # subdomain
    assert is_disposable_email("x@temp-mail.org")
    assert not is_disposable_email("x@gmail.com")
    assert not is_disposable_email("x@company.io")
    assert not is_disposable_email(None)
    assert not is_disposable_email("not-an-email")


def test_campaign_start_rejects_disposable_email(supabase_mock):
    from fastapi.testclient import TestClient

    from app.deps import get_current_user
    from app.main import app

    class DisposableUser:
        id = "00000000-0000-0000-0000-000000000002"
        email = "farm@10minutemail.com"

    app.dependency_overrides[get_current_user] = lambda: DisposableUser()
    try:
        res = TestClient(app).post(
            "/api/v1/campaign/start",
            json={"keywords": ["python"], "platforms": ["indeed"]},
        )
    finally:
        app.dependency_overrides.clear()
    assert res.status_code == 403
    assert res.json()["detail"] == "disposable_email"
