"""Interview kit — the prep sheet generated once per application and cached.

The risk this file covers: the AI slot must be claimed before the paid call and refunded
on every path that fails to produce a kit, or a failed generation silently eats the user's
day. The kit is built from jobs.description, whose preservation is covered separately in
test_save_job_preserves_scraped.py.
"""

from unittest.mock import patch

import pytest

from modules import ai_interview_kit as kit_mod

APP_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def app_row():
    return {
        "id": APP_ID,
        "status": "interview",
        "title": "Backend Engineer",
        "company": "Acme",
        "platform": "greenhouse",
        "link": "https://example.com/job",
        "description": "We need Python and Postgres experience.",
        "location": "Remote",
    }


# ── generation refuses to invent when material is missing ────────────────────


def test_generate_returns_none_without_resume():
    with patch.object(kit_mod, "load_resume_text", return_value=""):
        assert kit_mod.generate_interview_kit({"description": "x"}, {}) is None


def test_generate_returns_none_without_job_text():
    with patch.object(kit_mod, "load_resume_text", return_value="resume"):
        assert kit_mod.generate_interview_kit({"description": "   "}, {}) is None


def test_normalize_drops_malformed_entries():
    kit = kit_mod._normalize(
        {
            "company_brief": "not a dict",
            "questions": [
                {"q": "Real?", "bullets": ["a"], "proof": "10x"},
                {"bullets": ["orphan bullet with no question"]},
                "junk",
            ],
            "gaps": [{"gap": "Kubernetes", "say": "Haven't used it in production"}, {}],
            "ask_them": ["What does success look like?", ""],
        }
    )
    assert kit["company_brief"] == {"one_liner": "", "facts": []}
    assert [q["q"] for q in kit["questions"]] == ["Real?"]
    assert len(kit["gaps"]) == 1
    assert kit["ask_them"] == ["What does success look like?"]


def test_extract_json_handles_fenced_reply():
    assert kit_mod._extract_json('```json\n{"your_angle": "x"}\n```') == {"your_angle": "x"}


# ── endpoint: quota is claimed before the call and refunded when it fails ────


def test_get_is_side_effect_free_when_no_kit(auth_client, app_row):
    with (
        patch("app.db.interview_kit.get_kit", return_value=None),
        patch("app.db.applications.get_for_interview_kit", return_value=app_row),
        patch("app.db.usage.claim_daily_ai_slot") as claim,
    ):
        res = auth_client.get(f"/api/v1/applications/{APP_ID}/interview-kit")

    assert res.status_code == 200
    assert res.json()["ready"] is False
    assert res.json()["can_generate"] is True
    claim.assert_not_called()


def test_get_reports_missing_job_text(auth_client, app_row):
    app_row["description"] = ""
    with (
        patch("app.db.interview_kit.get_kit", return_value=None),
        patch("app.db.applications.get_for_interview_kit", return_value=app_row),
    ):
        res = auth_client.get(f"/api/v1/applications/{APP_ID}/interview-kit")

    assert res.json()["can_generate"] is False
    assert res.json()["reason"]


def test_get_404_for_someone_elses_application(auth_client):
    with (
        patch("app.db.interview_kit.get_kit", return_value=None),
        patch("app.db.applications.get_for_interview_kit", return_value=None),
    ):
        res = auth_client.get(f"/api/v1/applications/{APP_ID}/interview-kit")

    assert res.status_code == 404


def test_post_returns_cached_kit_without_spending_a_slot(auth_client, app_row):
    cached = {"payload": {"your_angle": "cached"}, "schema_version": 1, "created_at": "now"}
    with (
        patch("app.db.interview_kit.get_kit", return_value=cached),
        patch("app.db.applications.get_for_interview_kit", return_value=app_row),
        patch("app.db.usage.claim_daily_ai_slot") as claim,
    ):
        res = auth_client.post(f"/api/v1/applications/{APP_ID}/interview-kit")

    assert res.status_code == 200
    assert res.json()["kit"]["your_angle"] == "cached"
    claim.assert_not_called()


def test_ready_response_carries_the_header(auth_client, app_row):
    """The prep screen renders role/company from the same payload in both states."""
    cached = {"payload": {"your_angle": "x"}, "schema_version": 1, "created_at": "now"}
    with (
        patch("app.db.interview_kit.get_kit", return_value=cached),
        patch("app.db.applications.get_for_interview_kit", return_value=app_row),
    ):
        res = auth_client.get(f"/api/v1/applications/{APP_ID}/interview-kit")

    body = res.json()
    assert body["ready"] is True
    assert body["title"] == "Backend Engineer"
    assert body["company"] == "Acme"


def test_post_429_when_daily_ai_limit_reached(auth_client, app_row):
    with (
        patch("app.db.interview_kit.get_kit", return_value=None),
        patch("app.db.applications.get_for_interview_kit", return_value=app_row),
        patch("app.db.profile.get_profile", return_value={"resume_url": "r.pdf"}),
        patch("app.db.usage.claim_daily_ai_slot", return_value=False),
    ):
        res = auth_client.post(f"/api/v1/applications/{APP_ID}/interview-kit")

    assert res.status_code == 429


def test_post_refunds_slot_when_generation_raises(auth_client, app_row):
    with (
        patch("app.db.interview_kit.get_kit", return_value=None),
        patch("app.db.applications.get_for_interview_kit", return_value=app_row),
        patch("app.db.profile.get_profile", return_value={"resume_url": "r.pdf"}),
        patch("app.db.usage.claim_daily_ai_slot", return_value=True),
        patch("app.db.usage.release_today") as release,
        patch("modules.ai_interview_kit.generate_interview_kit", side_effect=RuntimeError("boom")),
    ):
        res = auth_client.post(f"/api/v1/applications/{APP_ID}/interview-kit")

    assert res.status_code == 500
    release.assert_called_once()


def test_post_refunds_slot_when_material_is_insufficient(auth_client, app_row):
    with (
        patch("app.db.interview_kit.get_kit", return_value=None),
        patch("app.db.applications.get_for_interview_kit", return_value=app_row),
        patch("app.db.profile.get_profile", return_value={"resume_url": "r.pdf"}),
        patch("app.db.usage.claim_daily_ai_slot", return_value=True),
        patch("app.db.usage.release_today") as release,
        patch("modules.ai_interview_kit.generate_interview_kit", return_value=None),
    ):
        res = auth_client.post(f"/api/v1/applications/{APP_ID}/interview-kit")

    assert res.status_code == 422
    release.assert_called_once()


def test_post_requires_a_resume(auth_client, app_row):
    with (
        patch("app.db.interview_kit.get_kit", return_value=None),
        patch("app.db.applications.get_for_interview_kit", return_value=app_row),
        patch("app.db.profile.get_profile", return_value={}),
        patch("app.db.usage.claim_daily_ai_slot") as claim,
    ):
        res = auth_client.post(f"/api/v1/applications/{APP_ID}/interview-kit")

    assert res.status_code == 422
    assert res.json()["error"] == "no_resume"
    claim.assert_not_called()


def test_post_caches_the_generated_kit(auth_client, app_row):
    payload = {"your_angle": "fresh"}
    with (
        patch("app.db.interview_kit.get_kit", return_value=None),
        patch("app.db.applications.get_for_interview_kit", return_value=app_row),
        patch("app.db.profile.get_profile", return_value={"resume_url": "r.pdf"}),
        patch("app.db.usage.claim_daily_ai_slot", return_value=True),
        patch("modules.ai_interview_kit.generate_interview_kit", return_value=payload),
        patch("app.db.interview_kit.save_kit") as save,
    ):
        save.return_value = {"payload": payload, "schema_version": 1, "created_at": "now"}
        res = auth_client.post(f"/api/v1/applications/{APP_ID}/interview-kit")

    assert res.status_code == 200
    assert res.json()["kit"] == payload
    save.assert_called_once()


# ── the free taste is the whole product, prep sheets included ────────────────
# Igor 09-12: 40 applications free, then data + interview kit + the rest are what the
# subscription buys. Enforced server-side — a paywall that only exists in the UI isn't one.


def _post_kit(auth_client, app_row, *, tier, free_used, free_limit=40):
    with (
        patch("app.db.interview_kit.get_kit", return_value=None),
        patch("app.db.applications.get_for_interview_kit", return_value=app_row),
        patch("app.db.profile.get_profile", return_value={"resume_url": "r.pdf"}),
        patch("app.db.subscriptions.get_tier", return_value=tier),
        patch("app.db.subscriptions.get_free_apps_used", return_value=free_used),
        patch("config.FREE_APP_LIMIT", free_limit),
        patch("app.db.usage.claim_daily_ai_slot", return_value=True) as claim,
        patch("modules.ai_interview_kit.generate_interview_kit", return_value={"your_angle": "x"}),
        patch("app.db.interview_kit.save_kit", return_value=None),
    ):
        res = auth_client.post(f"/api/v1/applications/{APP_ID}/interview-kit")
    return res, claim


def test_free_user_past_the_taste_cannot_build_a_kit(auth_client, app_row):
    res, claim = _post_kit(auth_client, app_row, tier="free", free_used=40)
    assert res.status_code == 402
    assert res.json()["error"] == "free_taste_spent"
    # The refusal must land BEFORE the paid call — otherwise the paywall costs us money.
    claim.assert_not_called()


def test_free_user_inside_the_taste_still_builds(auth_client, app_row):
    res, _ = _post_kit(auth_client, app_row, tier="free", free_used=39)
    assert res.status_code == 200


def test_paid_user_is_never_gated(auth_client, app_row):
    """A subscriber's free counter is irrelevant — it can read anything and must not gate."""
    res, _ = _post_kit(auth_client, app_row, tier="pro", free_used=999)
    assert res.status_code == 200
