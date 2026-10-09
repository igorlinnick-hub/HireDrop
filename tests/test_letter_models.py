"""Comparing cover-letter models (scripts/measure_letter_models.py). The comparison decides
which model writes every letter, so its isolation, its failure reporting and its arithmetic
are tested without spending anything."""

import os
import subprocess
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from modules import ai_cover_letter
from scripts import measure_letter_models as letters

JOB = {"title": "Project Manager", "company": "Acme", "description": "Run delivery. " * 40}
PROFILE = {"name": "Jordan", "email": "jordan@example.com"}


def _message(text="Dear Acme team,\n\nI run delivery.\n\nJordan", stop_reason="end_turn"):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=text)],
        usage=types.SimpleNamespace(input_tokens=1000, output_tokens=300),
        stop_reason=stop_reason,
    )


class FakeClient:
    """Answers per model; records every request it was sent."""

    def __init__(self, replies):
        self.replies = replies
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        reply = self.replies[kwargs["model"]]
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_the_default_is_what_ships_against_the_newer_sonnet():
    assert letters.parse_models(None) == [ai_cover_letter.COVER_LETTER_MODEL, "claude-sonnet-5-5"]
    assert letters.parse_models("claude-sonnet-4-6, claude-haiku-5-5") == [
        "claude-sonnet-4-6",
        "claude-haiku-5-5",
    ]


@pytest.mark.parametrize("spec", ["claude-sonnet-4-6", "claude-sonnet-4-6,claude-sonnet-4-6"])
def test_one_model_is_not_a_comparison(spec):
    with pytest.raises(SystemExit):
        letters.parse_models(spec)


def test_a_model_without_a_price_is_refused_before_spending():
    with pytest.raises(SystemExit, match="claude-unknown-9"):
        letters.parse_models("claude-sonnet-4-6,claude-unknown-9")


def test_each_model_gets_the_same_prompt_and_its_price_from_the_ledger():
    client = FakeClient({"claude-sonnet-4-6": _message(), "claude-sonnet-5-5": _message()})
    pairs = letters.write_letters(
        [JOB], PROFILE, "RESUME TEXT", ["claude-sonnet-4-6", "claude-sonnet-5-5"], client
    )

    old, new = client.requests
    assert old["messages"] == new["messages"] and old["system"] == new["system"]
    assert "RESUME TEXT" in old["messages"][0]["content"]
    got = pairs[0]["letters"]
    # 1000 in + 300 out at $3/$15 and at $2/$10 per million.
    assert got["claude-sonnet-4-6"]["usd"] == pytest.approx(0.0075)
    assert got["claude-sonnet-5-5"]["usd"] == pytest.approx(0.005)
    assert got["claude-sonnet-5-5"]["text"].startswith("Dear Acme team")


def test_a_refusal_and_an_api_error_are_failures_though_the_product_writes_the_template():
    client = FakeClient(
        {
            "claude-sonnet-4-6": RuntimeError("400 thinking.type: disabled is not supported"),
            "claude-sonnet-5-5": _message(text="", stop_reason="refusal"),
        }
    )
    pairs = letters.write_letters(
        [JOB], PROFILE, "RESUME", ["claude-sonnet-4-6", "claude-sonnet-5-5"], client
    )

    got = pairs[0]["letters"]
    template = ai_cover_letter.fallback_template(JOB, PROFILE)
    assert got["claude-sonnet-4-6"]["text"] == template
    assert "thinking.type" in got["claude-sonnet-4-6"]["error"]
    assert got["claude-sonnet-5-5"]["text"] == template
    assert got["claude-sonnet-5-5"]["error"] == "refused"

    summary = letters.summarize(pairs, ["claude-sonnet-4-6", "claude-sonnet-5-5"])
    assert summary["claude-sonnet-4-6"]["letters"] == 0
    assert summary["claude-sonnet-4-6"]["errors"] == 1
    assert summary["claude-sonnet-5-5"]["letters"] == 0
    assert summary["claude-sonnet-5-5"]["first_error"] == "refused"


def test_a_letter_cut_off_at_max_tokens_is_not_counted_as_written():
    client = FakeClient(
        {
            "claude-sonnet-4-6": _message(),
            "claude-sonnet-5-5": _message(
                text="Dear Acme team,\n\nI run", stop_reason="max_tokens"
            ),
        }
    )
    pairs = letters.write_letters(
        [JOB], PROFILE, "RESUME", ["claude-sonnet-4-6", "claude-sonnet-5-5"], client
    )

    summary = letters.summarize(pairs, ["claude-sonnet-4-6", "claude-sonnet-5-5"])
    assert summary["claude-sonnet-4-6"]["letters"] == 1
    assert summary["claude-sonnet-5-5"]["letters"] == 0
    assert summary["claude-sonnet-5-5"]["first_error"] == "cut off at max_tokens"
    assert "vs_baseline" not in summary["claude-sonnet-5-5"]


def test_a_model_that_cannot_answer_without_thinking_is_refused_before_spending():
    # Opus 5.5 is priced, but its thinking cannot be turned off.
    with pytest.raises(SystemExit, match="plain answer"):
        letters.parse_models("claude-sonnet-4-6,claude-opus-5-5")


def test_importing_the_measurement_helpers_leaves_the_spend_meter_on():
    # A fresh process: in this one the scripts are long imported.
    code = (
        "import os, scripts.measure_scorer_models, scripts.measure_judge_calibration; "
        "print(os.environ.get('AI_METER'))"
    )
    env = {k: v for k, v in os.environ.items() if k != "AI_METER"}
    root = os.path.join(os.path.dirname(__file__), "..")
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "None"


def test_the_product_is_left_as_it_was():
    before = (
        ai_cover_letter.COVER_LETTER_MODEL,
        ai_cover_letter.get_anthropic_client,
        ai_cover_letter.resume_text_for,
    )
    client = FakeClient({"claude-sonnet-4-6": _message(), "claude-haiku-5-5": _message()})
    letters.write_letters([JOB], PROFILE, "R", ["claude-sonnet-4-6", "claude-haiku-5-5"], client)
    after = (
        ai_cover_letter.COVER_LETTER_MODEL,
        ai_cover_letter.get_anthropic_client,
        ai_cover_letter.resume_text_for,
    )
    assert after == before


def test_the_others_are_compared_with_the_first():
    def pair(old_usd, new_usd):
        row = {"text": "x", "chars": 1, "in": 1, "out": 1, "refused": False, "error": None}
        return {"job": JOB, "letters": {"a": {**row, "usd": old_usd}, "b": {**row, "usd": new_usd}}}

    summary = letters.summarize([pair(0.010, 0.006), pair(0.012, 0.008)], ["a", "b"])

    assert summary["a"]["usd"] == pytest.approx(0.011)
    cmp = summary["b"]["vs_baseline"]
    assert cmp["delta_usd"] == pytest.approx(-0.004)
    assert cmp["ratio"] == pytest.approx(0.007 / 0.011)
    assert cmp["per_month_at_cap"] == pytest.approx(-0.004 * 900)


def test_postings_are_the_users_own_with_a_real_description_once_each():
    thin = {"job_title": "Welder", "company": "Thin Co", "jobs": {"description": "short"}}
    full = {"job_title": "PM", "company": "Acme", "jobs": {"description": "d" * 400}}
    again = {"job_title": "pm", "company": "ACME", "jobs": {"description": "e" * 400}}
    other = {
        "job_title": None,
        "company": None,
        "jobs": {"title": "Lead", "company": "Beta", "description": "f" * 400},
    }
    db = MagicMock()
    query = db.table.return_value.select.return_value
    query.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [
        thin,
        full,
        again,
        other,
    ]

    with patch("app.db.client.get_supabase", return_value=db):
        got = letters.applied_postings("user-1", 5)

    query.eq.assert_called_once_with("user_id", "user-1")
    assert [(j["company"], j["title"]) for j in got] == [("Acme", "PM"), ("Beta", "Lead")]
