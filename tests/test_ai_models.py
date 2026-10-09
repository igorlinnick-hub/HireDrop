"""Asking a 5.x model for a plain answer, and reading it back (modules/ai_models.py).

The cheaper 5.x models are what the cost work moves the judge and the letters to. Each of
them can open a reply with a thinking block, spends max_tokens on thinking unless told not
to, and can decline; the code that reads their replies must survive all three.
"""

import json
import types

import pytest

from modules import ai_cover_letter, ai_fit_judge, ai_job_scorer, ai_question_answer
from modules.ai_models import plain_answer_kwargs, refused, reply_text


@pytest.mark.parametrize(
    ("model", "thinking"),
    [
        ("claude-sonnet-5-5", {"type": "between_tools"}),
        ("claude-haiku-5-5", {"type": "disabled"}),
        ("claude-sonnet-5", {"type": "disabled"}),
        ("claude-haiku-4-5-20251001", None),
        ("claude-sonnet-4-6", None),
    ],
)
def test_each_model_gets_the_one_thinking_off_setting_it_accepts(model, thinking):
    assert plain_answer_kwargs(model).get("thinking") == thinking


def _reply(text, *, thinking_first=True, stop_reason="end_turn"):
    blocks = [types.SimpleNamespace(type="thinking", thinking="", signature="s")] * thinking_first
    blocks.append(types.SimpleNamespace(type="text", text=text))
    return types.SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        model="claude-haiku-5-5",
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def test_a_reply_that_opens_with_thinking_is_read_from_its_text_block():
    assert reply_text(_reply("hello")) == "hello"
    assert not refused(_reply("hello"))
    assert refused(_reply("", stop_reason="refusal"))


def _judge(monkeypatch, reply):
    sent = []

    class _Messages:
        def create(self, **kwargs):
            sent.append(kwargs)
            return reply(kwargs["model"])

    monkeypatch.setattr(ai_fit_judge, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(ai_fit_judge, "_CASCADE_ON", False)
    monkeypatch.setattr(ai_fit_judge, "_JUDGE_MODEL", "claude-haiku-5-5")
    monkeypatch.setattr(
        ai_fit_judge, "get_anthropic_client", lambda: types.SimpleNamespace(messages=_Messages())
    )
    monkeypatch.setattr(ai_fit_judge, "resume_text_for", lambda *a, **k: "resume text")
    verdict = ai_fit_judge.assess_fit(
        job={"title": "Welder", "company": "Acme", "description": "d"},
        profile={"apply_mode": "standard"},
    )
    return verdict, sent


def test_the_judge_reads_a_haiku_5_5_verdict_and_asks_it_not_to_think(monkeypatch):
    body = json.dumps({"fit_score": 80, "decision": "apply", "reason": "fits", "concerns": []})
    verdict, sent = _judge(monkeypatch, lambda _m: _reply(body))
    assert verdict["judged"] and verdict["fit_score"] == 80
    assert sent[0]["thinking"] == {"type": "disabled"}


def test_a_declined_judgement_is_no_verdict_not_a_score(monkeypatch):
    verdict, _ = _judge(monkeypatch, lambda _m: _reply("", stop_reason="refusal"))
    assert not verdict.get("judged")


def test_a_sonnet_5_5_letter_is_written_without_thinking_and_read_past_it(monkeypatch):
    sent = []

    class _Messages:
        def create(self, **kwargs):
            sent.append(kwargs)
            return _reply("Dear team,\n\nI build things.\n\nBest,\nAlex")

    monkeypatch.setattr(ai_cover_letter, "COVER_LETTER_MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(
        ai_cover_letter,
        "get_anthropic_client",
        lambda: types.SimpleNamespace(messages=_Messages()),
    )
    monkeypatch.setattr(ai_cover_letter, "resume_text_for", lambda *a, **k: "resume text")
    letter = ai_cover_letter.generate_cover_letter(
        {"title": "Engineer", "company": "Acme", "description": "Build things."},
        {"name": "Alex"},
    )
    assert sent[0]["thinking"] == {"type": "between_tools"}
    assert "I build things." in letter


def _screener(monkeypatch, reply):
    sent = []

    class _Messages:
        def create(self, **kwargs):
            sent.append(kwargs)
            return reply

    monkeypatch.setattr(ai_question_answer, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(ai_question_answer, "SCREENER_MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(
        ai_question_answer,
        "get_anthropic_client",
        lambda: types.SimpleNamespace(messages=_Messages()),
    )
    monkeypatch.setattr(ai_question_answer, "resume_text_for", lambda *a, **k: "resume text")
    answer = ai_question_answer.answer_screener_question(
        "How many years of welding experience do you have?",
        job={"title": "Welder", "company": "Acme", "description": "MIG welding."},
        options=["0-2", "3-5", "6+"],
    )
    return answer, sent


def test_a_sonnet_5_5_screener_answer_is_asked_without_thinking_and_read_past_it(monkeypatch):
    answer, sent = _screener(monkeypatch, _reply("3-5"))
    assert sent[0]["thinking"] == {"type": "between_tools"}
    assert answer == "3-5"


def test_a_declined_screener_answer_leaves_the_field_blank(monkeypatch):
    answer, _ = _screener(monkeypatch, _reply("3-5", stop_reason="refusal"))
    assert answer == ""


def _score(monkeypatch, reply):
    sent = []

    class _Messages:
        def create(self, **kwargs):
            sent.append(kwargs)
            return reply

    monkeypatch.setattr(ai_job_scorer, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(ai_job_scorer, "HAIKU_MODEL", "claude-haiku-5-5")
    monkeypatch.setattr(
        ai_job_scorer, "get_anthropic_client", lambda: types.SimpleNamespace(messages=_Messages())
    )
    scored = ai_job_scorer.score_job(
        {"title": "Welder", "company": "Acme", "description": "MIG welding, 3 years."},
        {"keywords": ["welder"]},
        "MIG welder, 5 years.",
    )
    return scored, sent


def test_a_haiku_5_5_score_is_asked_without_thinking_and_read_past_it(monkeypatch):
    body = json.dumps(
        {
            "score": 9,
            "verdict": "подходит",
            "reasons": ["MIG"],
            "flags": [],
            "ats_keywords": ["MIG"],
        }
    )
    scored, sent = _score(monkeypatch, _reply(body))
    assert sent[0]["thinking"] == {"type": "disabled"}
    assert scored["score"] == 9 and scored["ats_keywords"] == ["MIG"]


def test_a_declined_score_is_the_neutral_default_and_says_so(monkeypatch, capsys):
    scored, _ = _score(monkeypatch, _reply("", stop_reason="refusal"))
    assert scored == ai_job_scorer._default_score()
    assert "declined" in capsys.readouterr().out
