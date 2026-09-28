"""build_readiness truth table — every start precondition the server can know."""

from app.db.campaign import build_readiness

# Every question modules/employer_answers asks, answered — a profile that is ready.
ANSWERED = {
    "country": "United States",
    "city": "Honolulu",
    "state": "HI",
    "work_authorized_us": True,
    "needs_sponsorship": False,
    "current_title": "Marketing Manager",
    "current_employer": "Acme",
    "linkedin_url": "https://linkedin.com/in/x",
}


def _profile(**over):
    base = {
        "onboarding_completed": True,
        "keywords": ["marketing"],
        "platforms": ["indeed"],
        "resume_url": "https://x/resume.pdf",
        **ANSWERED,
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
    ready, checks = _ready(
        build_readiness(_profile(onboarding_completed=False), False, "pro", "auto", None, 40)
    )
    assert ready is False and checks["onboarding"] is False


def test_ats_platform_requires_resume():
    ready, checks = _ready(
        build_readiness(
            _profile(platforms=["greenhouse"], resume_url=None), False, "pro", "auto", None, 40
        )
    )
    assert ready is False and checks["resume"] is False


def test_board_platform_needs_no_resume():
    ready, checks = _ready(
        build_readiness(
            _profile(platforms=["indeed"], resume_url=None), False, "pro", "auto", None, 40
        )
    )
    assert checks["resume"] is True and ready is True


def test_lever_no_longer_blocks_the_start():
    """A board only SOME of the run can use is not a precondition for the run.

    Lever's captcha needs a human, so an auto run cannot finish one — but blocking Start
    over it offered a single button, "switch to Tap", which trades the whole auto campaign
    for one board out of six (Igor, 09-15). /campaign/start drops Lever from an auto run
    and says so in the feed instead; readiness stays about the campaign as a whole.
    """
    ready, checks = _ready(
        build_readiness(_profile(platforms=["lever"]), False, "pro", "auto", None, 40)
    )
    assert "lever_tap" not in checks
    assert ready is True


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


def test_unanswered_employer_questions_block_and_name_what_is_missing():
    res = build_readiness(
        _profile(country="", needs_sponsorship=None), False, "pro", "auto", None, 40
    )
    ready, by_id = _ready(res)
    assert not ready and by_id["employer_answers"] is False
    check = next(c for c in res["checks"] if c["id"] == "employer_answers")
    assert [m["key"] for m in check["missing"]] == ["country", "needs_sponsorship"]
    assert check["fix"] == "answers"


def test_false_is_an_answer_for_yes_no_questions():
    """'No, I don't need sponsorship' must not read as 'never answered'."""
    ready, _ = _ready(
        build_readiness(
            _profile(work_authorized_us=False, needs_sponsorship=False),
            False,
            "pro",
            "auto",
            None,
            40,
        )
    )
    assert ready


def test_state_is_asked_only_for_a_us_address_and_no_linkedin_is_an_answer():
    from modules.employer_answers import missing

    abroad = {**ANSWERED, "country": "Canada", "state": ""}
    assert missing(abroad) == []
    assert [m["key"] for m in missing({**ANSWERED, "state": ""})] == ["state"]
    assert missing({**ANSWERED, "linkedin_url": "", "no_linkedin": True}) == []


def test_clean_keeps_known_keys_typed_and_ignores_the_rest():
    from modules.employer_answers import clean

    out = clean(
        {
            "country": "  Canada ",
            "needs_sponsorship": "yes",
            "work_authorized_us": False,
            "tier": "pro",
            "city": "x" * 999,
            "no_linkedin": True,
        }
    )
    assert out["country"] == "Canada"
    assert "needs_sponsorship" not in out  # a string is not a yes/no answer
    assert out["work_authorized_us"] is False
    assert "tier" not in out
    assert len(out["city"]) == 200 and out["no_linkedin"] is True
