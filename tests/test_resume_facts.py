"""Facts offered at signup come from the resume VERBATIM, or not at all.

The form confirms what the user's own resume says. A value the model made up — a
university the candidate never attended, a title one level above the real one — would be
confirmed by a tired person clicking Continue and then filed on every application.
"""

import json
from unittest.mock import MagicMock, patch

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
    # "CA" is inside "eduCAtion" and "LA" inside "cLAss" — letters are not a fact.
    assert facts.grounded({"city": "Kharkiv", "state": "CA"}, RESUME) == {}
    assert facts.grounded({"state": "ON"}, RESUME) == {}


def test_unknown_keys_and_non_strings_are_dropped():
    assert facts.grounded({"tier": "pro", "school": ["MIT"], "degree": None}, RESUME) == {}
    assert facts.grounded(["Marketing Lead"], RESUME) == {}
    assert facts.grounded({"school": "   "}, RESUME) == {}


def _reply(text: str) -> MagicMock:
    client = MagicMock()
    client.messages.create.return_value = MagicMock(content=[MagicMock(text=text)])
    return client


def test_extract_grounds_the_model_reply():
    reply = json.dumps({"current_title": "Marketing Lead", "school": "Harvard University"})
    with (
        patch.object(facts, "ANTHROPIC_API_KEY", "k"),
        patch.object(facts, "get_anthropic_client", return_value=_reply(f"```json\n{reply}\n```")),
    ):
        assert facts.extract_facts(RESUME) == {"current_title": "Marketing Lead"}


def test_extract_never_raises_and_never_calls_without_a_resume():
    client = _reply("not json")
    with (
        patch.object(facts, "ANTHROPIC_API_KEY", "k"),
        patch.object(facts, "get_anthropic_client", return_value=client),
    ):
        assert facts.extract_facts(RESUME) == {}
        client.messages.create.reset_mock()
        assert facts.extract_facts("   ") == {}
        client.messages.create.assert_not_called()
    with patch.object(facts, "ANTHROPIC_API_KEY", ""):
        assert facts.extract_facts(RESUME) == {}
