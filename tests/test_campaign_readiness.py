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
    "school": "University of Hawaii at Manoa",
    "degree": "BA Communications",
    "salary_expectation": "$100,000 per year",
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


def test_no_linkedin_is_an_answer():
    from modules.employer_answers import missing

    assert missing({**ANSWERED, "linkedin_url": "", "no_linkedin": True}) == []
    assert [m["key"] for m in missing({**ANSWERED, "state": ""})] == ["state"]


def test_living_outside_the_us_closes_start_with_no_fix():
    """US only (Igor 09-27): a No is a closed door, not a form to fill."""
    res = build_readiness(_profile(country="Outside US"), False, "pro", "auto", None, 40)
    ready, by_id = _ready(res)
    assert not ready and by_id["us_only"] is False
    row = next(c for c in res["checks"] if c["id"] == "us_only")
    assert row["fix"] is None
    # A legacy free-text country from Settings is judged the same way.
    assert (
        _ready(build_readiness(_profile(country="Germany"), False, "pro", "auto", None, 40))[1][
            "us_only"
        ]
        is False
    )
    assert _ready(build_readiness(_profile(country="USA"), False, "pro", "auto", None, 40))[0]


def test_clean_keeps_known_keys_typed_and_ignores_the_rest():
    from modules.employer_answers import clean

    out = clean(
        {
            "country": "Canada",  # free text is not an answer — only Yes/No is
            "needs_sponsorship": "yes",
            "work_authorized_us": False,
            "tier": "pro",
            "city": "x" * 999,
            "no_linkedin": True,
        }
    )
    assert "country" not in out
    assert clean({"country": True})["country"] == "United States"
    assert clean({"country": False})["country"] == "Outside US"
    assert "needs_sponsorship" not in out  # a string is not a yes/no answer
    assert out["work_authorized_us"] is False
    assert "tier" not in out
    assert len(out["city"]) == 200 and out["no_linkedin"] is True


def test_no_degree_answers_both_education_questions():
    from modules.employer_answers import missing

    blank = {**ANSWERED, "school": "", "degree": ""}
    assert [m["key"] for m in missing(blank)] == ["school", "degree"]
    assert missing({**blank, "no_degree": True}) == []


RESUME = {
    "contact": {"linkedin": "linkedin.com/in/jane"},
    "experience": [
        {"title": "Social Media Manager", "company": "Acme"},
        {"title": "Intern", "company": "Older Co"},
    ],
    "education": [{"degree": "BA Communications", "school": "UT Austin", "year": "2018"}],
}


def test_missing_offers_what_the_resume_already_says():
    """Offered for the person to confirm — the profile itself stays blank until they do."""
    from modules.employer_answers import missing

    profile = {**ANSWERED, "school": "", "degree": "", "city": "", "ats_structure": RESUME}
    by_key = {m["key"]: m for m in missing(profile)}
    assert by_key["school"]["suggestion"] == "UT Austin"
    assert by_key["degree"]["suggestion"] == "BA Communications"
    # The "I don't have one" tickbox travels with the question it answers.
    assert by_key["school"]["opt_out"] == {
        "flag": "no_degree",
        "label": "I don't have a college degree",
    }
    assert "opt_out" not in by_key["city"]
    # The resume has no fact for a city question, so nothing is offered.
    assert "suggestion" not in by_key["city"]
    assert profile["school"] == ""


def test_form_lists_every_question_with_the_answer_on_file():
    """Signup draws ALL of it, in the server's order, in the form's own terms."""
    from modules.employer_answers import QUESTIONS, form

    rows = form(
        {**ANSWERED, "current_title": "", "needs_sponsorship": None, "ats_structure": RESUME}
    )
    assert [r["key"] for r in rows] == [k for k, _, _ in QUESTIONS]
    by_key = {r["key"]: r for r in rows}
    assert by_key["country"]["value"] is True  # "United States" -> Yes
    assert by_key["work_authorized_us"]["value"] is True
    assert by_key["needs_sponsorship"]["value"] is None
    assert by_key["current_title"] == {
        "key": "current_title",
        "label": "Most recent job title",
        "kind": "text",
        "value": "",
        "suggestion": "Social Media Manager",
    }
    # An answer on file is never second-guessed by the resume.
    assert "suggestion" not in by_key["school"]
    assert form({"country": "Outside US"})[0]["value"] is False
    assert form({})[0]["value"] is None


def test_suggestions_survive_a_malformed_structure():
    from modules.employer_answers import suggestions

    assert suggestions({"ats_structure": None}) == {}
    assert suggestions({"ats_structure": {"education": "MIT", "experience": [None]}}) == {}
    assert suggestions({"ats_structure": {"education": [{"school": "  "}]}}) == {}


def test_clean_takes_both_opt_outs():
    from modules.employer_answers import clean

    out = clean({"no_degree": True, "no_linkedin": "yes", "school": " MIT ", "degree": "BS"})
    assert out == {"school": "MIT", "degree": "BS", "no_degree": True, "no_linkedin": False}


def test_the_salary_floor_is_offered_never_filed():
    """`salary_min` filters which jobs the user sees. As an EXPECTATION it is a guess —
    shown in the form for them to confirm, with a way to decline naming one at all."""
    from modules.employer_answers import missing

    blank = {**ANSWERED, "salary_expectation": ""}
    assert [m["key"] for m in missing(blank)] == ["salary_expectation"]
    row = missing({**blank, "salary_min": 100_000})[0]
    assert row["suggestion"] == "$100,000 per year"
    assert row["opt_out"]["flag"] == "no_salary_expectation"
    assert "suggestion" not in missing({**blank, "salary_min": None})[0]
    # True is not a salary (bool is an int in Python).
    assert "suggestion" not in missing({**blank, "salary_min": True})[0]
    assert missing({**blank, "no_salary_expectation": True}) == []
