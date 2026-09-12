"""The apply-mode dial is the bar — pin it so a model's own verdict can't overrule the user.

Live run 09-11 (broad mode, bar 35): the judge returned fit_score 42 with decision "skip"
three times, the extension gates on `decision` alone, and the day ended with zero
applications. Two authorities decided one thing; now only the bar does. The escape hatch
for a genuine hard blocker is the score itself (0-34, below every bar), and the prompt
says so — these tests hold both halves in place.
"""

import json
import types

import pytest

from modules import ai_fit_judge


def _judge(monkeypatch, *, score, decision, mode="broad"):
    """Run assess_fit against a canned model reply."""
    payload = json.dumps(
        {"fit_score": score, "decision": decision, "reason": "canned", "concerns": []}
    )

    class _Messages:
        def create(self, **kwargs):
            _Messages.last_system = kwargs["system"]
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=payload)])

    client = types.SimpleNamespace(messages=_Messages())
    monkeypatch.setattr(ai_fit_judge, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(ai_fit_judge, "get_anthropic_client", lambda: client)
    monkeypatch.setattr(ai_fit_judge, "load_resume_text", lambda _url: "resume text")
    result = ai_fit_judge.assess_fit(
        job={"title": "Project Manager", "company": "Corvant", "description": "d"},
        profile={"apply_mode": mode, "keywords": ["project manager"]},
    )
    result["_system"] = _Messages.last_system
    return result


@pytest.mark.parametrize("mode,bar", [("broad", 35), ("standard", 55), ("precise", 70)])
def test_at_or_above_the_bar_applies_even_when_the_model_says_skip(monkeypatch, mode, bar):
    r = _judge(monkeypatch, score=bar, decision="skip", mode=mode)
    assert r["decision"] == "apply"
    assert r["threshold"] == bar
    # The disagreement stays visible instead of being swallowed by the reconciliation.
    assert r["model_decision"] == "skip"


def test_the_live_case_that_produced_zero_applications(monkeypatch):
    # Broad mode, score 42: the exact shape logged three times on 09-11.
    r = _judge(monkeypatch, score=42, decision="skip", mode="broad")
    assert r["decision"] == "apply"


@pytest.mark.parametrize("mode,bar", [("broad", 35), ("standard", 55), ("precise", 70)])
def test_below_the_bar_skips_even_when_the_model_says_apply(monkeypatch, mode, bar):
    r = _judge(monkeypatch, score=bar - 1, decision="apply", mode=mode)
    assert r["decision"] == "skip"


def test_unparseable_score_still_fails_closed(monkeypatch):
    r = _judge(monkeypatch, score="not-a-number", decision="apply", mode="broad")
    assert r["fit_score"] == 0
    assert r["decision"] == "skip"


def test_an_unusable_mode_falls_back_to_standard(monkeypatch):
    r = _judge(monkeypatch, score=40, decision="apply", mode="whatever")
    assert r["apply_mode"] == "standard"
    assert r["threshold"] == 55
    assert r["decision"] == "skip"


@pytest.mark.parametrize("mode,bar", [("broad", 35), ("standard", 55), ("precise", 70)])
def test_the_prompt_names_the_bar_so_score_and_decision_stop_disagreeing(mode, bar):
    p = ai_fit_judge._system_prompt(mode, bar)
    assert f"fit_score >= {bar}" in p
    assert mode.upper() in p
    # The only sanctioned way to express a hard blocker is a score below every bar.
    assert "0-34" in p


def test_a_judge_that_cannot_run_still_skips():
    r = ai_fit_judge._fallback({"title": "x"})
    assert r["decision"] == "skip" and r["fail_closed"] is True
