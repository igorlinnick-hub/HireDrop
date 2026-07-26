"""build_readiness truth table — every start precondition the server can know."""

from app.db.campaign import build_readiness


def _profile(**over):
    base = {
        "onboarding_completed": True,
        "keywords": ["marketing"],
        "platforms": ["indeed"],
        "resume_url": "https://x/resume.pdf",
    }
    base.update(over)
    return base


def _ready(res):
    return res["ready"], {c["id"]: c["ok"] for c in res["checks"]}


def test_happy_path_is_ready():
    ready, _ = _ready(build_readiness(_profile(), False, "pro", "auto", None, 40))
    assert ready is True


def test_no_keywords_blocks():
    ready, checks = _ready(build_readiness(_profile(keywords=[]), False, "pro", "auto", None, 40))
    assert ready is False and checks["keywords"] is False


def test_onboarding_blocks():
    ready, checks = _ready(build_readiness(_profile(onboarding_completed=False), False, "pro", "auto", None, 40))
    assert ready is False and checks["onboarding"] is False


def test_ats_platform_requires_resume():
    ready, checks = _ready(build_readiness(
        _profile(platforms=["greenhouse"], resume_url=None), False, "pro", "auto", None, 40))
    assert ready is False and checks["resume"] is False


def test_board_platform_needs_no_resume():
    ready, checks = _ready(build_readiness(
        _profile(platforms=["indeed"], resume_url=None), False, "pro", "auto", None, 40))
    assert checks["resume"] is True and ready is True


def test_lever_requires_tap():
    ready, checks = _ready(build_readiness(
        _profile(platforms=["lever"]), False, "pro", "auto", None, 40))
    assert ready is False and checks["lever_tap"] is False
    ready2, checks2 = _ready(build_readiness(
        _profile(platforms=["lever"]), False, "pro", "tap", None, 40))
    assert checks2["lever_tap"] is True


def test_free_quota_blocks_when_exhausted():
    ready, checks = _ready(build_readiness(_profile(), False, "free", "auto", 40, 40))
    assert ready is False and checks["free_quota"] is False


def test_free_quota_passes_with_remaining():
    ready, checks = _ready(build_readiness(_profile(), False, "free", "auto", 12, 40))
    assert checks["free_quota"] is True and ready is True


def test_paid_tier_has_no_free_check():
    res = build_readiness(_profile(), False, "pro", "auto", None, 40)
    assert "free_quota" not in {c["id"] for c in res["checks"]}


def test_running_campaign_blocks():
    ready, checks = _ready(build_readiness(_profile(), True, "pro", "auto", None, 40))
    assert ready is False and checks["not_running"] is False


def test_failed_checks_carry_reason_and_fix():
    res = build_readiness(_profile(keywords=[]), False, "pro", "auto", None, 40)
    kw = next(c for c in res["checks"] if c["id"] == "keywords")
    assert kw["reason"] and kw["fix"] == "keywords"
    ok = next(c for c in res["checks"] if c["id"] == "onboarding")
    assert ok["reason"] is None and ok["fix"] is None
