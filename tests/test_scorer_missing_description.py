"""A job we know nothing about must not outrank a job we can actually check.

Measured in prod 2026-09-15 (1307 rows): jobs WITHOUT a description averaged
score 2.93, jobs WITH one averaged 1.67, and 24 of the 26 rows that ever cleared
the tailoring gate (`jobs.score >= 7`) were description-less LinkedIn postings —
a platform we cannot even submit to.

The cause was in _format_job: when the description was empty it dropped the line
entirely, so the model saw a title, a company, and no indication that anything
was missing. It scored the title match, and a title match alone looks great.

`jobs.score` orders the job list and gates resume tailoring, so this inversion
put unverifiable jobs at the top of both.
"""

import json
import types

from modules import ai_job_scorer

JOB = {"title": "Project Manager", "company": "Corvant", "location": "Austin, TX"}
PROFILE = {"keywords": ["project manager"]}


def test_missing_description_is_stated_not_dropped():
    """The prompt must carry the absence — silence reads as 'nothing to worry about'."""
    text = ai_job_scorer._format_job(JOB)
    assert "NOT AVAILABLE" in text
    assert "cannot be verified" in text or "Nothing about its requirements" in text


def test_present_description_is_passed_through_unchanged():
    text = ai_job_scorer._format_job({**JOB, "description": "Own the roadmap."})
    assert "Own the roadmap." in text
    assert "NOT AVAILABLE" not in text


def test_scoring_prompt_caps_the_unverifiable(monkeypatch):
    """The instruction has to reach the model, not just the docstring."""
    import sys

    seen = {}

    class _Messages:
        def create(self, **kwargs):
            seen["prompt"] = kwargs["messages"][0]["content"]
            return types.SimpleNamespace(
                content=[
                    types.SimpleNamespace(
                        text=json.dumps(
                            {
                                "score": 5,
                                "verdict": "сомнительно",
                                "reasons": [],
                                "flags": ["no description"],
                                "ats_keywords": [],
                            }
                        )
                    )
                ]
            )

    monkeypatch.setattr(ai_job_scorer, "ANTHROPIC_API_KEY", "test-key")
    # score_job imports anthropic INSIDE the function, so the module table is the
    # only hook. setitem (not a bare assignment) so the fake cannot leak into any
    # other test in the session.
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        types.SimpleNamespace(Anthropic=lambda **_: types.SimpleNamespace(messages=_Messages())),
    )

    result = ai_job_scorer.score_job(JOB, PROFILE)

    prompt = seen["prompt"]
    assert "NOT AVAILABLE" in prompt, "the model must be told the description is missing"
    assert "score at most 5" in prompt, "and told what that means for the score"
    assert result["score"] == 5
