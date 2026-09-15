"""The fit-judge cascade must save money without moving the bar.

The judge runs on every candidate job, so it was the single most expensive call per
application. It now screens with Haiku and escalates only scores near the user's bar,
where a small model being slightly off would flip the verdict.

What these tests hold in place:
  - a confident score is decided by the CHEAP model and Sonnet is never called;
  - a score near the bar is re-judged by Sonnet, and SONNET'S score is the one returned;
  - the verdict still comes from `score >= threshold` — the cascade must not become a
    second authority over the dial (the bug test_fit_gate.py exists to prevent);
  - a Haiku failure costs money, never correctness: it falls through to Sonnet rather
    than fail-closed skipping a job the good model would have approved.
"""

import json
import types

from modules import ai_fit_judge


def _run(monkeypatch, replies, *, mode="standard", band=15):
    """Drive assess_fit with a canned reply per model. Records the call order.

    `replies` maps a model id to the dict it should return, or to None for a failure.
    """
    calls = []

    class _Messages:
        def create(self, **kwargs):
            model = kwargs["model"]
            calls.append(model)
            reply = replies.get(model)
            if reply is None:
                raise RuntimeError(f"{model} is down")
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=json.dumps(reply))])

    monkeypatch.setattr(ai_fit_judge, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(ai_fit_judge, "_CASCADE_ON", True)
    monkeypatch.setattr(ai_fit_judge, "_CASCADE_BAND", band)
    monkeypatch.setattr(
        ai_fit_judge, "get_anthropic_client", lambda: types.SimpleNamespace(messages=_Messages())
    )
    monkeypatch.setattr(ai_fit_judge, "load_resume_text", lambda _url: "resume text")

    result = ai_fit_judge.assess_fit(
        job={"title": "Project Manager", "company": "Corvant", "description": "d"},
        profile={"apply_mode": mode},
    )
    return result, calls


def _verdict(score, decision="apply"):
    return {"fit_score": score, "decision": decision, "reason": "canned", "concerns": []}


HAIKU = ai_fit_judge._SCREEN_MODEL
SONNET = ai_fit_judge._JUDGE_MODEL


def test_confident_yes_never_reaches_sonnet(monkeypatch):
    """Standard bar 55, band 15 -> 90 is far above it. Haiku decides, Sonnet unpaid."""
    result, calls = _run(monkeypatch, {HAIKU: _verdict(90), SONNET: _verdict(10)})
    assert calls == [HAIKU]
    assert result["fit_score"] == 90
    assert result["decision"] == "apply"
    assert result["judge_model"] == HAIKU
    assert result["escalated"] is False


def test_confident_no_never_reaches_sonnet(monkeypatch):
    result, calls = _run(monkeypatch, {HAIKU: _verdict(12, "skip"), SONNET: _verdict(99)})
    assert calls == [HAIKU]
    assert result["fit_score"] == 12
    assert result["decision"] == "skip"


def test_score_near_the_bar_is_rejudged_and_sonnet_wins(monkeypatch):
    """58 is inside 55 +/- 15, so the cheap opinion is discarded, not blended."""
    result, calls = _run(monkeypatch, {HAIKU: _verdict(58), SONNET: _verdict(31, "skip")})
    assert calls == [HAIKU, SONNET]
    assert result["fit_score"] == 31, "Sonnet's score must replace Haiku's, not average with it"
    assert result["decision"] == "skip"
    assert result["judge_model"] == SONNET
    assert result["escalated"] is True


def test_band_edge_escalates(monkeypatch):
    """Exactly `band` away is still 'near' — the comparison is strict on the far side."""
    _, calls = _run(monkeypatch, {HAIKU: _verdict(70), SONNET: _verdict(70)}, band=15)
    assert calls == [HAIKU, SONNET]


def test_bar_still_owns_the_verdict_after_a_cheap_decision(monkeypatch):
    """Haiku saying "skip" at 90 must not skip: the dial decides, the model advises.

    This is test_fit_gate.py's invariant, re-pinned on the cascade path — a cheap
    model's stray verdict word is exactly as powerless as an expensive one's.
    """
    result, calls = _run(monkeypatch, {HAIKU: _verdict(90, "skip"), SONNET: _verdict(0)})
    assert calls == [HAIKU]
    assert result["decision"] == "apply"
    assert result["model_decision"] == "skip"


def test_broad_mode_moves_the_band_with_the_bar(monkeypatch):
    """Bar 35: a 58 is confidently ABOVE it, so broad mode decides 58 cheaply."""
    result, calls = _run(monkeypatch, {HAIKU: _verdict(58), SONNET: _verdict(0)}, mode="broad")
    assert calls == [HAIKU]
    assert result["decision"] == "apply"


def test_screen_failure_falls_through_to_sonnet(monkeypatch):
    """A Haiku outage must cost money, not correctness — never a fail-closed skip."""
    result, calls = _run(monkeypatch, {HAIKU: None, SONNET: _verdict(80)})
    assert calls == [HAIKU, SONNET]
    assert result["judged"] is True
    assert result["fit_score"] == 80
    assert result["decision"] == "apply"


def test_unparseable_cheap_score_falls_through_to_sonnet(monkeypatch):
    """Prose instead of a number is 'we learned nothing', not 'score 0'."""
    result, calls = _run(
        monkeypatch, {HAIKU: {"fit_score": "maybe", "decision": "apply"}, SONNET: _verdict(80)}
    )
    assert calls == [HAIKU, SONNET]
    assert result["fit_score"] == 80


def test_both_models_down_fails_closed(monkeypatch):
    """The safety property survives the cascade: no verdict means skip, not apply."""
    result, calls = _run(monkeypatch, {HAIKU: None, SONNET: None})
    assert calls == [HAIKU, SONNET]
    assert result["decision"] == "skip"
    assert result["judged"] is False
    assert result["fail_closed"] is True


def test_cascade_off_uses_sonnet_only(monkeypatch):
    """The kill switch must bypass the cheap call entirely, not just ignore it."""
    monkeypatch.setattr(ai_fit_judge, "_CASCADE_ON", False)
    calls = []

    class _Messages:
        def create(self, **kwargs):
            calls.append(kwargs["model"])
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(text=json.dumps(_verdict(90)))]
            )

    monkeypatch.setattr(ai_fit_judge, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        ai_fit_judge, "get_anthropic_client", lambda: types.SimpleNamespace(messages=_Messages())
    )
    monkeypatch.setattr(ai_fit_judge, "load_resume_text", lambda _url: "resume text")
    ai_fit_judge.assess_fit(job={"title": "t", "company": "c"}, profile={"apply_mode": "standard"})
    assert calls == [SONNET]
