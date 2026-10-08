"""Facts the person told us once — remembered, reused, and mentioned in letters where they fit."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules import ai_cover_letter as cl
from modules import personal_facts as pf

# ── which questions are about the person's circumstances ─────────────────────────────


def test_relocation_and_location_questions_are_circumstances():
    assert pf.topic_of("Are you willing to relocate to Miami, FL?") == "relocation"
    assert pf.topic_of("Are you open to relocation?") == "relocation"
    assert pf.topic_of("Do you currently live in or near San Diego?") == "location"
    assert pf.topic_of("Are you located within 30 miles of Austin, TX?") == "location"
    assert pf.topic_of("Where are you currently located?") == "location"
    assert pf.topic_of("Are you able to work on-site 5 days a week?") == "onsite"
    assert pf.topic_of("Are you willing to travel up to 25%?") == "travel"
    assert pf.topic_of("Can you work weekends?") == "schedule"
    assert pf.topic_of("When can you start?") == "start_date"


def test_the_shared_table_python_side():
    """The extension classifies the same way (content.js circumstanceTopic) — both run
    chrome-extension/tests/fixtures/circumstance-topics.json; extend the table, not a copy."""
    import json
    from pathlib import Path

    table = (
        Path(__file__).parent.parent / "chrome-extension/tests/fixtures/circumstance-topics.json"
    )
    for question, topic in json.loads(table.read_text()):
        assert pf.topic_of(question) == topic, question


def test_experience_questions_that_mention_a_topic_word_are_not():
    # The resume answers these — they must keep going to the normal answerer.
    assert pf.topic_of("Describe your experience working night shifts") is None
    assert pf.topic_of("Do you have experience in the travel industry?") is None
    assert pf.topic_of("Why do you want to work in our San Diego office?") is None
    assert pf.topic_of("Years of Python experience") is None
    assert pf.topic_of("Location") is None  # a field, filled from the profile client-side


# ── remembering ──────────────────────────────────────────────────────────────────────


def test_same_question_replaces_its_answer_and_keeps_its_id():
    facts, first = pf.upsert([], {"question": "Willing to relocate to Miami?", "answer": "Yes"})
    # The drift forms really show: case, and the required-asterisk.
    facts, again = pf.upsert(facts, {"question": "WILLING TO RELOCATE TO MIAMI?*", "answer": "No"})
    assert again["id"] == first["id"]
    assert [(f["id"], f["answer"]) for f in facts] == [(first["id"], "No")]


def test_replace_drops_the_earlier_answer_the_person_chose_to_replace():
    facts, sd = pf.upsert(
        [], {"question": "Relocation plans", "answer": "Moving to San Diego in December"}
    )
    facts, _ = pf.upsert(
        facts,
        {"question": "Are you willing to relocate to Miami?", "answer": "Yes"},
        replace_ids=[sd["id"]],
    )
    assert [f["question"] for f in facts] == ["Are you willing to relocate to Miami?"]


def test_related_shows_earlier_answers_on_the_same_topic_only():
    facts, sd = pf.upsert([], {"question": "Relocation plans", "answer": "San Diego, December"})
    facts, _ = pf.upsert(facts, {"question": "Can you work weekends?", "answer": "Yes"})
    rel = pf.related(facts, pf.topic_of("Are you willing to relocate to Miami?"))
    assert [f["id"] for f in rel] == [sd["id"]]


def test_a_stored_answer_answers_only_the_same_question():
    facts, _ = pf.upsert(
        [], {"question": "Are you willing to relocate to San Diego?", "answer": "Yes"}
    )
    assert (
        pf.match("Are you willing to relocate to San Diego? *", ["Yes", "No"], facts)["answer"]
        == "Yes"
    )
    # Another city is another question: the model (or the person) decides, never this match.
    assert pf.match("Are you willing to relocate to Miami?", ["Yes", "No"], facts) is None


def test_answer_snaps_to_the_forms_own_wording_or_not_at_all():
    assert (
        pf.snap("Yes", ["Yes, I am willing to relocate", "No, I am not"])
        == "Yes, I am willing to relocate"
    )
    assert pf.snap("no", ["Yes", "No"]) == "No"
    assert pf.snap("Maybe later", ["Yes", "No"]) is None
    assert pf.snap("Yes", ["Yes, onsite", "Yes, hybrid", "No"]) is None  # ambiguous: ask
    assert pf.snap("Anything", []) == "Anything"


def test_the_list_is_bounded_and_garbage_is_dropped():
    raw = [{"question": f"Q{i}", "answer": "A"} for i in range(60)] + [
        "x",
        {"question": "no answer"},
    ]
    facts = pf.clean_facts(raw)
    assert len(facts) == pf.MAX_FACTS
    assert all(f["id"].startswith("f_") for f in facts)


def test_fingerprint_changes_with_an_answer_and_is_empty_without_facts():
    assert pf.fingerprint([]) == ""
    a, _ = pf.upsert([], {"question": "Weekends?", "answer": "Yes"})
    b, _ = pf.upsert([], {"question": "Weekends?", "answer": "No"})
    assert pf.fingerprint(a) and pf.fingerprint(a) != pf.fingerprint(b)


# ── the letter: job location + "mention where it fits" ─────────────────────────────


def _letter_prompt(job: dict, profile: dict) -> str:
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(text="Hi.")])
    with (
        patch.object(cl, "ANTHROPIC_API_KEY", "k"),
        patch.object(cl, "get_anthropic_client", return_value=client),
        patch.object(cl, "resume_text_for", return_value="Marketing, clinic."),
    ):
        cl.generate_cover_letter(job, profile)
    return client.messages.create.call_args.kwargs["messages"][0]["content"]


JOB = {"title": "Marketing Manager", "company": "Acme", "description": "x" * 300}


def test_the_letter_writer_sees_where_the_job_is():
    prompt = _letter_prompt({**JOB, "location": "San Diego, CA"}, {})
    assert "Job Location: San Diego, CA" in prompt
    assert "Job Location: not stated" in _letter_prompt(JOB, {})


def test_letter_notes_reach_the_writer_with_the_rule_for_when():
    facts, _ = pf.upsert(
        [],
        {
            "question": "Relocation plans",
            "answer": "Moving to San Diego in December",
            "in_letters": True,
        },
    )
    facts, _ = pf.upsert(facts, {"question": "Can you work weekends?", "answer": "Yes"})
    prompt = _letter_prompt({**JOB, "location": "San Diego, CA"}, {"personal_facts": facts})
    assert "Relocation plans: Moving to San Diego in December" in prompt
    assert "Leave it out for a remote job" in prompt
    # Only facts the person marked for letters — a screener answer is not letter material.
    assert "weekends" not in prompt.lower()


def test_no_notes_no_block():
    assert cl.letter_notes_block({}) == ""
    assert "WHERE THEY MATTER" not in _letter_prompt(JOB, {"personal_facts": []})


def test_preview_endpoint_reads_location_from_the_pool_when_the_walk_had_none():
    from app.routers import tools

    seen = {}

    def fake_generate(job, profile):
        seen.update(job)
        return "letter"

    user = SimpleNamespace(id="u1", email="a@b.c")
    req = tools.LetterPreviewRequest(
        keywords="Marketing Manager, Acme",
        job_title="Marketing Manager",
        company="Acme",
        job_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    with (
        patch.object(tools, "get_profile", return_value={}),
        patch.object(tools, "_claim_ai_slot"),
        patch.object(tools, "generate_cover_letter", side_effect=fake_generate),
        patch.object(
            tools.jobs_db, "get_by_link", return_value={"location": "San Diego, CA"}
        ) as by_link,
    ):
        tools.cover_letter_preview(req, user)
    by_link.assert_called_once_with("u1", "https://boards.greenhouse.io/acme/jobs/1")
    assert seen["location"] == "San Diego, CA"
    assert seen["company"] == "Acme"  # not "your company" any more when the name is known


def test_preview_endpoint_prefers_the_location_the_walk_read():
    from app.routers import tools

    seen = {}
    user = SimpleNamespace(id="u1", email="a@b.c")
    req = tools.LetterPreviewRequest(keywords="x", job_location="Remote", job_url="https://x")
    with (
        patch.object(tools, "get_profile", return_value={}),
        patch.object(tools, "_claim_ai_slot"),
        patch.object(
            tools, "generate_cover_letter", side_effect=lambda j, p: seen.update(j) or "l"
        ),
        patch.object(tools.jobs_db, "get_by_link") as by_link,
    ):
        tools.cover_letter_preview(req, user)
    by_link.assert_not_called()
    assert seen["location"] == "Remote"


def test_questions_about_this_employer_are_never_reused_for_the_next():
    assert not pf.reusable("Have you applied to this company before?", ["Yes", "No"])
    assert not pf.reusable("Are you willing to work in our Austin office?", ["Yes", "No"])
    assert not pf.reusable("Why do you want to join us?")
    assert pf.reusable("Are you willing to relocate to Miami, FL?")
    assert pf.reusable("Do you have a valid driver's license?", ["Yes", "No"])
    assert not pf.reusable("Tell us about a project you led")  # open essay
    facts, _ = pf.upsert(
        [], {"question": "Have you applied to this company before?", "answer": "No"}
    )
    assert pf.match("Have you applied to this company before?", ["Yes", "No"], facts) is None
