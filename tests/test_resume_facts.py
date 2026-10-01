"""Facts offered at signup come from the resume VERBATIM, or not at all.

The form confirms what the user's own resume says. A value the model made up — a
university the candidate never attended, a title one level above the real one — would be
confirmed by a tired person clicking Continue and then filed on every application.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from modules import ai_resume_facts as facts

RESUME = """IGOR LYNNIK
Honolulu, HI · linkedin.com/in/igor-lynnik

EXPERIENCE
Marketing Lead — Hawaii Wellness Clinic            2023 – Present
Marketing Coordinator — Acme Corp                  2020 – 2023

EDUCATION
BA Communications, University of Hawaii
at Manoa, 2019
"""


@pytest.fixture(autouse=True)
def _fresh_memo():
    facts._MEMO.clear()
    yield
    facts._MEMO.clear()


def test_only_values_the_resume_contains_survive():
    raw = {
        "current_title": "Marketing Lead",
        "current_employer": "Hawaii Wellness Clinic",
        "linkedin_url": "linkedin.com/in/igor-lynnik",
        # The PDF broke this across two lines — whitespace is not a difference.
        "school": "University of Hawaii at Manoa",
        # Not in the resume: the model "completed" the degree name.
        "degree": "Bachelor of Arts in Communications",
    }
    assert facts.grounded(raw, RESUME) == {
        "current_title": "Marketing Lead",
        "current_employer": "Hawaii Wellness Clinic",
        "linkedin_url": "linkedin.com/in/igor-lynnik",
        "school": "University of Hawaii at Manoa",
    }


def test_where_they_live_comes_from_the_contact_line_as_whole_words():
    assert facts.grounded({"city": "Honolulu", "state": "HI"}, RESUME) == {
        "city": "Honolulu",
        "state": "HI",
    }
    assert facts.grounded({"state": "TX"}, "Austin TX 78701") == {"state": "TX"}
    assert facts.grounded({"state": "Texas"}, "Austin, Texas") == {"state": "Texas"}
    # "CA" is inside "eduCAtion" — letters are not a fact.
    assert facts.grounded({"city": "Kharkiv", "state": "CA"}, RESUME) == {}
    # …and letters are letters in any alphabet: "MA" is inside "Mañana".
    assert facts.grounded({"state": "MA"}, "Mañana Media, Boston") == {}


@pytest.mark.parametrize(
    ("key", "value", "resume"),
    [
        # In the text, and still not that fact.
        ("school", "-", "Education - none"),
        ("degree", "•", "• Led a team"),
        # A state code that is also a word, written as the word.
        ("state", "IN", "Experience IN marketing, Austin, TX"),
        ("state", "OR", "Sales OR marketing roles, Austin, TX"),
        ("state", "HI", "Hi, I am a marketer based in Austin, TX"),
        ("linkedin_url", "jane-roe", "jane-roe · Austin, TX"),
        ("city", "Remote", "Remote · linkedin.com/in/x"),
    ],
)
def test_text_that_appears_but_is_not_that_fact_is_dropped(key, value, resume):
    assert facts.grounded({key: value}, resume) == {}


def test_typography_is_not_a_reason_to_drop_a_real_fact():
    """PDF text says "–" and "’", the model says "-" and "'" (or the other way round)."""
    resume = "Office Manager – St. Mary’s Hospital\nCertiﬁed Analyst, Stanford"
    assert facts.grounded(
        {"current_employer": "St. Mary's Hospital", "current_title": "Certified Analyst"}, resume
    ) == {"current_employer": "St. Mary's Hospital", "current_title": "Certified Analyst"}


def test_unknown_keys_and_non_strings_are_dropped():
    assert facts.grounded({"tier": "pro", "school": ["MIT"], "degree": None}, RESUME) == {}
    assert facts.grounded(["Marketing Lead"], RESUME) == {}
    assert facts.grounded({"school": "   "}, RESUME) == {}


def _reply(text: str) -> MagicMock:
    client = MagicMock()
    client.messages.create.return_value = MagicMock(content=[MagicMock(text=text)])
    return client


@pytest.mark.parametrize(
    "wrapping",
    ["{}", "```json\n{}\n```", "Here is the JSON:\n{}\nLet me know!", "﻿{}", "```JSON\n{}\n```"],
)
def test_extract_reads_the_object_whatever_surrounds_it(wrapping):
    reply = json.dumps({"current_title": "Marketing Lead", "school": "Harvard University"})
    with (
        patch.object(facts, "ANTHROPIC_API_KEY", "k"),
        patch.object(facts, "get_anthropic_client", return_value=_reply(wrapping.format(reply))),
    ):
        assert facts.extract_facts(RESUME) == {"current_title": "Marketing Lead"}


def test_no_answer_is_none_and_an_empty_answer_is_a_dict():
    """The difference is money: None lets the caller refund its quota slot, {} does not."""
    with patch.object(facts, "ANTHROPIC_API_KEY", "k"):
        with patch.object(facts, "get_anthropic_client", return_value=_reply("not json")):
            assert facts.extract_facts(RESUME) is None
        with patch.object(facts, "get_anthropic_client", side_effect=RuntimeError("503")):
            assert facts.extract_facts(RESUME) is None
        answered = json.dumps({"school": "Harvard University"})  # nothing of it is in the text
        with patch.object(facts, "get_anthropic_client", return_value=_reply(answered)):
            assert facts.extract_facts(RESUME) == {}
        client = _reply("{}")
        with patch.object(facts, "get_anthropic_client", return_value=client):
            assert facts.extract_facts("   ") is None
            client.messages.create.assert_not_called()
    with patch.object(facts, "ANTHROPIC_API_KEY", ""):
        assert facts.extract_facts(RESUME) is None


def test_one_resume_is_read_once():
    claimed: list[int] = []
    client = _reply(json.dumps({"current_title": "Marketing Lead"}))
    with (
        patch.object(facts, "ANTHROPIC_API_KEY", "k"),
        patch.object(facts, "get_anthropic_client", return_value=client),
    ):
        first = facts.facts_for("u1", RESUME, before_call=lambda: claimed.append(1))
        again = facts.facts_for("u1", RESUME, before_call=lambda: claimed.append(1))
        other_user = facts.facts_for("u2", RESUME, before_call=lambda: claimed.append(1))
        new_resume = facts.facts_for("u1", RESUME + "\nPMP", before_call=lambda: claimed.append(1))
    assert first == ({"current_title": "Marketing Lead"}, True)
    assert again == ({"current_title": "Marketing Lead"}, False)  # no call, no slot
    assert other_user[1] is True and new_resume[1] is True
    assert client.messages.create.call_count == 3 and len(claimed) == 3


def test_a_read_that_found_nothing_is_remembered_and_a_failed_one_is_not():
    with patch.object(facts, "ANTHROPIC_API_KEY", "k"):
        nothing = _reply(json.dumps({"school": "Harvard University"}))
        with patch.object(facts, "get_anthropic_client", return_value=nothing):
            assert facts.facts_for("u1", RESUME) == ({}, True)
            assert facts.facts_for("u1", RESUME) == ({}, False)
        assert nothing.messages.create.call_count == 1
        with patch.object(facts, "get_anthropic_client", side_effect=RuntimeError("503")):
            assert facts.facts_for("u9", RESUME) == (None, True)
        ok = _reply(json.dumps({"current_title": "Marketing Lead"}))
        with patch.object(facts, "get_anthropic_client", return_value=ok):
            assert facts.facts_for("u9", RESUME) == ({"current_title": "Marketing Lead"}, True)
