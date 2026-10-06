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

import pytest

from app.db import screener_cache

# The shape get_profile really returns: no `updated_at` (the old fixture had one, so
# the old key looked fine here and was blind in prod), and every upload at one path.
PROFILE = {
    "name": "X",
    "resume_url": "u1/resume.pdf",
    "default_resume": None,
    "ats_approved": False,
    "ats_structure": None,
}
OPTIONS = ["0-1 years", "2-4 years", "5+ years"]

# Storage as the key sees it: path -> eTag of the file there now.
FILES = {"u1/resume.pdf": '"etag-1"'}


def _storage_list(folder, opts):
    name = opts["search"]
    path = f"{folder}/{name}"
    if path not in FILES:
        return []
    return [{"name": name, "updated_at": "2026-09-01T00:00:00Z", "metadata": {"eTag": FILES[path]}}]


@pytest.fixture(autouse=True)
def _storage():
    FILES.clear()
    FILES["u1/resume.pdf"] = '"etag-1"'
    sb = MagicMock()
    sb.storage.from_.return_value.list.side_effect = _storage_list
    with patch("app.db.screener_cache.get_supabase", return_value=sb):
        yield sb


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


def _key(profile=PROFILE):
    return screener_cache.build_key("Years of experience?", OPTIONS, profile)


def test_a_new_resume_at_the_same_path_retires_the_answer():
    """Every upload overwrites <user_id>/resume.pdf — the file's content hash is what
    changes, so it is what the key must carry (the 10-06 bug: it carried neither)."""
    base = _key()
    FILES["u1/resume.pdf"] = '"etag-2"'
    assert _key() != base


def test_editing_the_ats_resume_retires_the_answer_only_when_it_is_the_one_read():
    structure = {"experience": [{"title": "PM", "years": 2}]}
    edited = {"experience": [{"title": "PM", "years": 6}]}
    ats = {**PROFILE, "default_resume": "ats", "ats_structure": structure}
    assert _key(ats) != _key({**ats, "ats_structure": edited})
    # On the original resume the structure is not read, so editing it changes nothing.
    original = {**PROFILE, "default_resume": "original", "ats_structure": structure}
    assert _key(original) == _key({**original, "ats_structure": edited})


def test_switching_the_default_resume_retires_the_answer():
    structure = {"experience": [{"title": "PM"}]}
    assert _key({**PROFILE, "ats_structure": structure}) != _key(
        {**PROFILE, "ats_structure": structure, "default_resume": "ats"}
    )
    # ats_approved without a dial value means the ATS resume (resume_text_for's rule).
    assert _key({**PROFILE, "ats_structure": structure, "ats_approved": True}) == _key(
        {**PROFILE, "ats_structure": structure, "default_resume": "ats"}
    )


def test_an_unrelated_profile_edit_keeps_the_answer():
    """Filter chips write the profile row all day; they don't change what the answerer
    reads, so they must not cost a fresh model call."""
    assert _key() == _key({**PROFILE, "keywords": ["designer"], "location": "Austin"})


def test_unknown_resume_means_answer_live_not_cache(_storage):
    FILES.clear()
    assert _key() is None  # the path names no file — don't key on a guess
    _storage.storage.from_.return_value.list.side_effect = RuntimeError("storage down")
    assert _key() is None


def test_the_key_reads_fields_get_profile_really_returns():
    """Guard against the fixture drifting from reality again: build the profile the way
    the endpoint does and check the key still moves with the resume."""
    from app.db import profile as profile_db

    row = {
        "user_id": "u1",
        "resume_url": "u1/resume.pdf",
        "default_resume": "ats",
        "ats_structure": {"experience": [{"title": "PM", "years": 2}]},
    }
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [row]
    with patch("app.db.profile.get_supabase", return_value=sb):
        before = profile_db.get_profile("u1")
        row["ats_structure"] = {"experience": [{"title": "PM", "years": 6}]}
        after = profile_db.get_profile("u1")
    assert _key(before) != _key(after)


def test_education_answers_retire_the_answer_but_their_absence_changes_nothing():
    """The degree the user confirmed reaches the prompt, so it must reach the key —
    and a profile that never had one keeps the key it was cached under."""
    base = screener_cache.build_key("Highest degree?", OPTIONS, PROFILE)
    assert base == screener_cache.build_key(
        "Highest degree?", OPTIONS, {**PROFILE, "school": "", "degree": "", "no_degree": False}
    )
    with_degree = screener_cache.build_key("Highest degree?", OPTIONS, {**PROFILE, "degree": "BS"})
    none = screener_cache.build_key("Highest degree?", OPTIONS, {**PROFILE, "no_degree": True})
    assert len({base, with_degree, none}) == 3


def test_work_status_flags_retire_the_answer_but_their_absence_changes_nothing():
    """work_authorized_us / needs_sponsorship are answered deterministically before the
    model runs (_status_from_profile) and that answer is cached like any other — a later
    change to either flag (visa status, say) must not keep serving the old Yes/No."""
    base = screener_cache.build_key("Are you authorized to work in the US?", ["Yes", "No"], PROFILE)
    assert base == screener_cache.build_key(
        "Are you authorized to work in the US?",
        ["Yes", "No"],
        {**PROFILE, "work_authorized_us": None, "needs_sponsorship": None},
    )
    authorized = screener_cache.build_key(
        "Are you authorized to work in the US?",
        ["Yes", "No"],
        {**PROFILE, "work_authorized_us": True},
    )
    not_authorized = screener_cache.build_key(
        "Are you authorized to work in the US?",
        ["Yes", "No"],
        {**PROFILE, "work_authorized_us": False},
    )
    needs_sponsor = screener_cache.build_key(
        "Are you authorized to work in the US?",
        ["Yes", "No"],
        {**PROFILE, "needs_sponsorship": True},
    )
    assert len({base, authorized, not_authorized, needs_sponsor}) == 4


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


def test_endpoint_hands_the_posting_text_to_the_model(auth_client):
    """The extension sends only title + company; with a job_id the server adds the
    posting text from the user's own pool row (scoped by user_id — IDOR rule)."""
    generate = MagicMock(return_value="Because of the role.")
    lookup = MagicMock(return_value={"description": "We automate taxes for small businesses."})
    with (
        patch("app.routers.tools.get_profile", return_value=PROFILE),
        patch("app.routers.tools.handbacks_db.answers_for_job", return_value=[]),
        patch("app.routers.tools.usage_db.claim_today", return_value=True),
        patch("app.routers.tools.answer_screener_question", generate),
        patch("app.routers.tools.jobs_db.get_job_by_id", lookup),
    ):
        auth_client.post(
            "/api/v1/tools/answer-question",
            json={
                "question": "Why us?",
                "options": [],
                "job_title": "PM",
                "company": "Found",
                "job_id": "j1",
            },
        )
    assert lookup.call_args.args[1] == "j1"
    assert generate.call_args.kwargs["job"]["description"].startswith("We automate taxes")


def test_endpoint_uses_sent_posting_text_without_a_pool_row(auth_client):
    """Indeed/ZR live search has no job_id; the extension can send the text itself."""
    generate = MagicMock(return_value="Because of the role.")
    with (
        patch("app.routers.tools.get_profile", return_value=PROFILE),
        patch("app.routers.tools.usage_db.claim_today", return_value=True),
        patch("app.routers.tools.answer_screener_question", generate),
    ):
        auth_client.post(
            "/api/v1/tools/answer-question",
            json={
                "question": "Why us?",
                "options": [],
                "job_title": "PM",
                "company": "X",
                "job_description": "We sell pest control.",
            },
        )
    assert generate.call_args.kwargs["job"]["description"] == "We sell pest control."
