"""Comparing job-scorer models (scripts/measure_scorer_models.py). The comparison decides
which model scores every new posting, so failures must count as failures and the
agreement arithmetic must be right — tested without spending anything."""

import json
import types

import pytest

from modules import ai_job_scorer
from scripts import measure_scorer_models as sm

ROW = {
    "id": "j1",
    "title": "Welder",
    "company": "Acme",
    "platform": "greenhouse",
    "description": "MIG and TIG welding on structural steel. " * 10,
}
OLD, NEW = "claude-haiku-4-5-20251001", "claude-haiku-5-5"


def _message(text):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=text)],
        usage=types.SimpleNamespace(input_tokens=700, output_tokens=200),
        stop_reason="end_turn",
    )


def _verdict(score, keywords=("MIG", "TIG")):
    return json.dumps(
        {
            "score": score,
            "verdict": "x",
            "reasons": ["r"],
            "flags": [],
            "ats_keywords": list(keywords),
        }
    )


class FakeClient:
    def __init__(self, replies):
        self.replies = replies
        self.messages = self

    def create(self, **kwargs):
        reply = self.replies[kwargs["model"]]
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(ai_job_scorer, "ANTHROPIC_API_KEY", "test-key")


def test_bands_follow_the_prompts_score_guide():
    assert [sm.band(s) for s in (10, 8, 7, 5, 4, 0)] == [
        "strong",
        "strong",
        "consider",
        "consider",
        "skip",
        "skip",
    ]


def test_keyword_overlap_ignores_case_and_two_empty_lists_agree():
    assert sm.jaccard(["MIG", "TIG"], ["mig", "Blueprints"]) == pytest.approx(1 / 3)
    assert sm.jaccard([], []) == 1.0


def test_both_models_score_the_same_row_and_are_priced_from_the_ledger():
    client = FakeClient({OLD: _message(_verdict(8)), NEW: _message(_verdict(7, ["mig"]))})
    scored = sm.score_rows([ROW], {"keywords": ["welder"]}, "resume", [OLD, NEW], client)

    old, new = scored[0]["scores"][OLD], scored[0]["scores"][NEW]
    assert (old["score"], new["score"]) == (8, 7)
    assert not old["error"] and not new["error"]
    # 700 in + 200 out at $1/$5 and at $0.10/$0.50 per million.
    assert old["usd"] == pytest.approx(0.0017)
    assert new["usd"] == pytest.approx(0.00017)
    assert scored[0]["job"]["described"]


def test_the_neutral_fallback_counts_as_a_failure_not_as_a_five():
    client = FakeClient(
        {OLD: RuntimeError("529 overloaded"), NEW: _message("I would say about an eight.")}
    )
    scored = sm.score_rows([ROW], {}, "resume", [OLD, NEW], client)

    old, new = scored[0]["scores"][OLD], scored[0]["scores"][NEW]
    assert "overloaded" in old["error"]
    assert new["error"] == "no usable score in the reply"
    assert sm.per_model(scored, NEW)["scored"] == 0
    assert sm.compare(scored, OLD, NEW) == {"rows": 0}


def test_the_product_is_left_as_it_was():
    before = (
        ai_job_scorer.get_anthropic_client,
        ai_job_scorer.HAIKU_MODEL,
        ai_job_scorer._default_score,
    )
    client = FakeClient({OLD: _message(_verdict(8)), NEW: _message(_verdict(8))})
    sm.score_rows([ROW], {}, "resume", [OLD, NEW], client)
    after = (
        ai_job_scorer.get_anthropic_client,
        ai_job_scorer.HAIKU_MODEL,
        ai_job_scorer._default_score,
    )
    assert after == before
    assert "fallback" not in ai_job_scorer._default_score()


def _scored(pairs, described=True):
    out = []
    for i, (old, new) in enumerate(pairs):
        row = {"reasons": [], "flags": [], "ats_keywords": ["a"], "in": 700, "out": 200}
        out.append(
            {
                "job": {
                    "id": i,
                    "title": "t",
                    "company": "c",
                    "platform": "p",
                    "described": described,
                },
                "scores": {
                    OLD: {**row, "score": old, "error": None, "usd": 0.001},
                    NEW: {**row, "score": new, "error": None, "usd": 0.0001},
                },
            }
        )
    return out


def test_agreement_flips_and_bias_are_counted_over_rows_both_scored():
    scored = _scored([(9, 9), (8, 7), (6, 4), (9, 3)])

    c = sm.compare(scored, OLD, NEW)

    assert c["rows"] == 4
    assert c["exact"] == 0.25
    assert c["within_1"] == 0.5
    assert c["same_band"] == 0.25  # 9/9 only: 8→7, 6→4 and 9→3 all change band
    assert c["mean_delta"] == pytest.approx((0 - 1 - 2 - 6) / 4)
    assert [r["job"]["id"] for r in c["flips"]] == [3]
    assert c["widest"][0]["job"]["id"] == 3


def test_the_report_splits_rows_with_and_without_a_description():
    scored = _scored([(9, 9), (8, 8)]) + _scored([(5, 2)], described=False)

    text = "\n".join(sm.report(scored, [OLD, NEW]))

    assert "all rows (3)" in text
    assert "with a description (2): same band 100%" in text
    assert "without one (1): same band 0%" in text
    assert "0.10x" in text


def test_rows_split_where_the_prompt_caps_the_score_at_any_description_at_all():
    client = FakeClient({OLD: _message(_verdict(5)), NEW: _message(_verdict(5))})
    rows = [
        {**ROW, "id": "short", "description": "Weld."},
        {**ROW, "id": "none", "description": ""},
    ]

    scored = sm.score_rows(rows, {}, "resume", [OLD, NEW], client)

    assert [r["job"]["described"] for r in scored] == [True, False]
