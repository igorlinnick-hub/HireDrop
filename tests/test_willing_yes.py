"""Willingness to the job's conditions (office days, relocation, travel, shifts) is
answered Yes before any model call; facts about the person are not."""

import pytest

from modules import ai_question_answer as qa


@pytest.mark.parametrize(
    ("question", "options", "want"),
    [
        (
            "Are you willing to work 5 days per week from our office in NYC or LA?",
            ["Yes.", "No. I want to work remote."],
            "Yes.",
        ),
        ("Are you open to relocating to Austin, TX?", ["No", "Yes"], "Yes"),
        ("Are you able to commute to our Boston office 3 days a week?", ["Yes", "No"], "Yes"),
        ("Would you be comfortable working weekends?", [], "Yes."),
        (
            "Are you willing to travel up to 25%?",
            ["I am willing to travel", "I am not willing"],
            "I am willing to travel",
        ),
        ("Can you work on-site in Miami?", ["Yes, I can", "No"], "Yes, I can"),
    ],
)
def test_willingness_is_yes(question, options, want):
    assert qa._willing_yes(question, options) == want


@pytest.mark.parametrize(
    ("question", "options"),
    [
        ("Are you currently located in the New York metro area?", ["Yes", "No"]),
        ("Do you require relocation assistance?", ["Yes", "No"]),
        ("How did you hear about this role?", []),
        ("What is your earliest start date?", []),
        ("Are you willing to relocate?", ["Remote only", "Not at this time"]),  # no Yes to give
    ],
)
def test_not_willingness_or_no_yes_option(question, options):
    assert qa._willing_yes(question, options) is None


def test_answer_skips_the_model(monkeypatch):
    monkeypatch.setattr(qa, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(qa, "get_anthropic_client", lambda: pytest.fail("model was called"))
    got = qa.answer_screener_question(
        "Are you willing to work 5 days per week from our office in NYC or LA?",
        options=["Yes.", "No. I want to work remote."],
        unattended=True,
    )
    assert got == "Yes."
