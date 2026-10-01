"""The screener answerer must read the posting, not guess the employer from its name.

Dry-run 09-27: "Why are you interested in joining Found?" was answered "Found is
building in the health and wellness space" — Found does small-business taxes; the
model only had the company NAME and borrowed the candidate's clinic background.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules import ai_question_answer as aq


def _capture_prompt(job, profile=None):
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(text="Because of the role.")]
    )
    with (
        patch.object(aq, "ANTHROPIC_API_KEY", "k"),
        patch.object(aq, "get_anthropic_client", return_value=client),
        patch.object(aq, "resume_text_for", return_value="Marketing manager, clinic."),
    ):
        aq.answer_screener_question(
            "Why are you interested in joining Found?", job=job, profile=profile or {}
        )
    kwargs = client.messages.create.call_args.kwargs
    return kwargs["messages"][0]["content"], kwargs["system"]


def test_posting_text_reaches_the_model():
    prompt, system = _capture_prompt(
        {
            "title": "Growth Lead",
            "company": "Found",
            "description": "We automate taxes  for\nsmall business owners.",
        }
    )
    assert "We automate taxes for small business owners." in prompt
    assert "EMPLOYER" in system


def test_posting_text_is_capped():
    prompt, _ = _capture_prompt({"title": "t", "company": "c", "description": "x" * 10000})
    assert "x" * aq._MAX_POSTING_CHARS in prompt
    assert "x" * (aq._MAX_POSTING_CHARS + 1) not in prompt


def test_missing_posting_is_said_not_blank():
    prompt, _ = _capture_prompt({"title": "t", "company": "c"})
    assert "Posting text: (not available)" in prompt


def test_education_the_candidate_confirmed_reaches_the_model():
    """A resume with no education section is not a candidate with no education: what
    they answered at signup is in the prompt, with the resume's standing."""
    job = {"title": "t", "company": "c"}
    prompt, _ = _capture_prompt(job, {"school": "Kharkiv National University", "degree": "B.S."})
    assert "Stated by the candidate directly" in prompt
    assert "School: Kharkiv National University" in prompt
    assert "Degree: B.S." in prompt

    prompt, _ = _capture_prompt(job, {"no_degree": True, "school": "ignored"})
    assert "The candidate has no college degree." in prompt
    assert "ignored" not in prompt

    prompt, _ = _capture_prompt(job, {"school": "", "degree": ""})
    assert "Stated by the candidate directly" not in prompt
