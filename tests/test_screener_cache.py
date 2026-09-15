"""Screener answers are cached per (user, question, options, profile).

Two things were wrong before the cache, and the tests below pin both fixes:

  MONEY — every occurrence of the same multiple-choice screener was a fresh Sonnet
  call. The cache must be consulted BEFORE the daily AI quota is claimed, otherwise
  repeats keep draining the budget and only the invoice improves.

  CONSISTENCY — regenerating meant one candidate could answer the same question two
  different ways on two applications. A cached answer is the same answer.

What must NOT be cached is equally load-bearing: open-ended questions ("why do you
want to work here?") are per-job by nature, and reusing one would send the same
paragraph to a different employer.
"""

from unittest.mock import MagicMock, patch

from app.db import screener_cache

PROFILE = {"name": "X", "resume_url": "r1.pdf", "updated_at": "2026-09-01T00:00:00Z"}
OPTIONS = ["0-1 years", "2-4 years", "5+ years"]


# --------------------------------------------------------------- key building


def test_open_question_is_never_cacheable():
    """No options = free text = job-specific. No key, so no cache path at all."""
    assert screener_cache.build_key("Why do you want to work here?", None, PROFILE) is None
    assert screener_cache.build_key("Why do you want to work here?", [], PROFILE) is None


def test_closed_question_gets_a_key():
    assert screener_cache.build_key("Years of experience?", OPTIONS, PROFILE)


def test_cosmetic_differences_collapse_to_one_key():
    """The same screener renders differently across boards; it is still one question."""
    base = screener_cache.build_key("Years of experience?", OPTIONS, PROFILE)
    for variant in (
        "years of experience?",
        "  Years   of experience?  ",
        "Years of experience? *",
        "Years of experience?(required)",
    ):
        assert screener_cache.build_key(variant, OPTIONS, PROFILE) == base, variant


def test_option_order_does_not_change_the_key():
    """Radio groups render in different orders; the choice set is what matters."""
    a = screener_cache.build_key("Years of experience?", OPTIONS, PROFILE)
    b = screener_cache.build_key("Years of experience?", list(reversed(OPTIONS)), PROFILE)
    assert a == b


def test_different_options_are_different_questions():
    """Same wording, different brackets to choose from -> a different answer is required."""
    a = screener_cache.build_key("Years of experience?", OPTIONS, PROFILE)
    b = screener_cache.build_key("Years of experience?", ["Yes", "No"], PROFILE)
    assert a != b


def test_different_subject_is_not_collapsed():
    """Normalisation must stay cosmetic: Python and Java are not the same question."""
    a = screener_cache.build_key("Years of experience with Python?", OPTIONS, PROFILE)
    b = screener_cache.build_key("Years of experience with Java?", OPTIONS, PROFILE)
    assert a != b


def test_editing_the_profile_retires_the_answer():
    """A new resume can change the right answer — stale reuse would be worse than a call."""
    base = screener_cache.build_key("Years of experience?", OPTIONS, PROFILE)
    newer = screener_cache.build_key(
        "Years of experience?", OPTIONS, {**PROFILE, "updated_at": "2026-09-20T00:00:00Z"}
    )
    reuploaded = screener_cache.build_key(
        "Years of experience?", OPTIONS, {**PROFILE, "resume_url": "r2.pdf"}
    )
    assert base != newer
    assert base != reuploaded


# ------------------------------------------------------------------- endpoint


def _post(client, question="Years of experience?", options=OPTIONS):
    return client.post(
        "/api/v1/tools/answer-question",
        json={"question": question, "options": options, "job_title": "PM", "company": "Corvant"},
    )


def test_cache_hit_answers_without_model_or_quota(auth_client):
    """The whole point: a repeat spends neither a Sonnet call nor a quota slot."""
    claim = MagicMock(return_value=True)
    generate = MagicMock()
    with (
        patch("app.routers.tools.get_profile", return_value=PROFILE),
        patch("app.routers.tools.usage_db.claim_today", claim),
        patch("app.routers.tools.answer_screener_question", generate),
        patch("app.routers.tools.screener_cache.get", return_value="2-4 years"),
        patch("app.routers.tools.screener_cache.touch"),
    ):
        res = _post(auth_client)
    assert res.status_code == 200
    assert res.json() == {"answer": "2-4 years", "cached": True}
    generate.assert_not_called()
    claim.assert_not_called(), "a cache hit must not consume the daily AI budget"


def test_cache_miss_generates_and_stores(auth_client):
    put = MagicMock()
    with (
        patch("app.routers.tools.get_profile", return_value=PROFILE),
        patch("app.routers.tools.RATE_LIMIT_ENFORCE", True),
        patch("app.routers.tools.usage_db.claim_today", return_value=True),
        patch("app.routers.tools.answer_screener_question", return_value="2-4 years"),
        patch("app.routers.tools.screener_cache.get", return_value=None),
        patch("app.routers.tools.screener_cache.put", put),
    ):
        res = _post(auth_client)
    assert res.json() == {"answer": "2-4 years", "cached": False}
    put.assert_called_once()
    assert put.call_args.args[3] == "2-4 years"


def test_open_question_is_generated_every_time(auth_client):
    """No key -> the cache is never read and never written, even on success."""
    get = MagicMock()
    put = MagicMock()
    with (
        patch("app.routers.tools.get_profile", return_value=PROFILE),
        patch("app.routers.tools.RATE_LIMIT_ENFORCE", True),
        patch("app.routers.tools.usage_db.claim_today", return_value=True),
        patch("app.routers.tools.answer_screener_question", return_value="Because reasons."),
        patch("app.routers.tools.screener_cache.get", get),
        patch("app.routers.tools.screener_cache.put", put),
    ):
        res = _post(auth_client, question="Why do you want to work here?", options=[])
    assert res.json()["answer"] == "Because reasons."
    get.assert_not_called()
    put.assert_not_called()


def test_empty_answer_is_not_cached(auth_client):
    """A model that went off-script returns "" — caching that would poison the key."""
    put = MagicMock()
    release = MagicMock()
    with (
        patch("app.routers.tools.get_profile", return_value=PROFILE),
        patch("app.routers.tools.RATE_LIMIT_ENFORCE", True),
        patch("app.routers.tools.usage_db.claim_today", return_value=True),
        patch("app.routers.tools.usage_db.release_today", release),
        patch("app.routers.tools.answer_screener_question", return_value=""),
        patch("app.routers.tools.screener_cache.get", return_value=None),
        patch("app.routers.tools.screener_cache.put", put),
    ):
        res = _post(auth_client)
    assert res.json()["answer"] == ""
    put.assert_not_called()
    release.assert_called_once(), "a failed generation still refunds its slot"


def test_a_broken_cache_never_breaks_answering(auth_client):
    """The cache is an optimisation. If Supabase is unhappy, we answer live."""
    with (
        patch("app.routers.tools.get_profile", return_value=PROFILE),
        patch("app.routers.tools.RATE_LIMIT_ENFORCE", True),
        patch("app.routers.tools.usage_db.claim_today", return_value=True),
        patch("app.routers.tools.answer_screener_question", return_value="2-4 years"),
        # screener_cache imported get_supabase by value, so this is the reference
        # that has to break — patching app.db.client would leave the cache working.
        patch("app.db.screener_cache.get_supabase", side_effect=RuntimeError("down")),
    ):
        res = _post(auth_client)
    assert res.status_code == 200
    assert res.json()["answer"] == "2-4 years"
