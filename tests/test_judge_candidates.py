"""Comparing a cheaper judge with the current one (scripts/measure_judge_calibration.py
--candidate). The comparison decides whether the product's judge model changes, so its
arithmetic and its isolation are tested without spending anything."""

import json
import types

import pytest

from modules import ai_fit_judge
from scripts import measure_judge_calibration as cal


def _row(job_id, decision, score, cost, candidate=None, judged=True):
    row = {
        "user_id": "u1",
        "user": "Igor",
        "job_id": job_id,
        "title": f"Role {job_id}",
        "company": "Acme",
        "decision": decision,
        "fit_score": score,
        "reason": "r",
        "cost_usd": cost,
        "judged": judged,
    }
    if candidate:
        row["candidate"] = candidate
    return row


def test_a_candidate_spec_names_one_model_or_a_cascade():
    assert cal.parse_candidate("claude-haiku-5-5") == ("claude-haiku-5-5", None)
    assert cal.parse_candidate("claude-haiku-5-5+claude-sonnet-5-5") == (
        "claude-haiku-5-5",
        "claude-sonnet-5-5",
    )


def test_the_judge_runs_the_candidate_for_one_phase_and_goes_back():
    before = (ai_fit_judge._SCREEN_MODEL, ai_fit_judge._JUDGE_MODEL, ai_fit_judge._CASCADE_ON)
    with cal.judge_config("claude-haiku-5-5", None):
        assert ai_fit_judge._JUDGE_MODEL == "claude-haiku-5-5"
        assert ai_fit_judge._CASCADE_ON is False
    assert before == (
        ai_fit_judge._SCREEN_MODEL,
        ai_fit_judge._JUDGE_MODEL,
        ai_fit_judge._CASCADE_ON,
    )


def test_disagreements_are_split_into_lost_and_wasted_applications():
    spec = "claude-haiku-5-5"
    baseline = [
        _row("a", "apply", 70, 0.007),
        _row("b", "apply", 60, 0.007),
        _row("c", "skip", 20, 0.002),
        _row("d", "skip", 50, 0.007),
    ]
    candidates = [
        _row("a", "apply", 72, 0.0005, spec),
        _row("b", "skip", 40, 0.0005, spec),  # an application the cheaper judge would lose
        _row("c", "skip", 25, 0.0005, spec),
        _row("d", "apply", 58, 0.0005, spec),  # one it would waste
        _row("e", "apply", 90, 0.0005, spec),  # not judged by the current judge: unpaired
    ]
    c = cal.compare(baseline, candidates, spec)
    assert c["paired"] == 4
    assert c["false_rejects"] == 1 and c["false_accepts"] == 1
    assert c["false_reject_share_of_passes"] == 0.5
    assert c["agree"] == 0.5
    assert c["cheaper_by"] == round(0.023 / 0.002, 1)
    assert [b["job_id"] for b, _ in c["false_reject_rows"]] == ["b"]


def test_a_candidate_the_judge_cannot_ask_plainly_is_refused_before_spending():
    with pytest.raises(SystemExit, match="plain answer"):
        cal.parse_candidate("claude-haiku-5-5+claude-opus-5-5")


def test_a_row_judged_on_a_resumed_run_is_no_longer_undecided():
    baseline = [_row("j1", "apply", 80, 0.01), _row("j2", "apply", 80, 0.01)]
    candidates = [
        _row("j1", None, None, 0, candidate="c", judged=False),
        _row("j2", None, None, 0, candidate="c", judged=False),
        _row("j1", "apply", 75, 0.001, candidate="c"),
    ]

    result = cal.compare(baseline, candidates, "c")

    assert result["paired"] == 1
    assert result["unjudged"] == 1


def test_another_candidates_rows_never_leak_into_the_comparison():
    baseline = [_row("a", "apply", 70, 0.007)]
    other = [_row("a", "skip", 10, 0.001, "claude-sonnet-5-5")]
    assert cal.compare(baseline, other, "claude-haiku-5-5")["paired"] == 0


def test_a_candidate_phase_judges_only_rows_the_current_judge_judged(tmp_path, monkeypatch):
    user = {
        "uid": "u1",
        "label": "Igor",
        "profile": {"apply_mode": "standard"},
        "funnel": {"cut": "14d"},
        "cands": [{"id": "a", "title": "t", "company": "c"}, {"id": "b"}],
    }
    seen = []

    def judge_one(u, job):
        seen.append(job["id"])
        return _row(job["id"], "apply", 70, 0.001)

    monkeypatch.setattr(cal, "judge_one", judge_one)
    monkeypatch.setitem(cal.SPENT, "usd", 0.0)
    out = tmp_path / "candidates.jsonl"
    cal.run_judge([user], str(out), 1.0, 1, "claude-haiku-5-5", {("u1", "a")})
    assert seen == ["a"]
    assert json.loads(out.read_text())["candidate"] == "claude-haiku-5-5"


def test_the_recorder_prices_a_new_model_from_the_ledger_table(monkeypatch):
    reply = types.SimpleNamespace(
        model="claude-haiku-5-5",
        content=[types.SimpleNamespace(type="text", text='{"fit_score": 80}')],
        usage=types.SimpleNamespace(input_tokens=1_000_000, output_tokens=0),
    )
    real = types.SimpleNamespace(messages=types.SimpleNamespace(create=lambda **kw: reply))
    monkeypatch.setattr(cal.ai_cover_letter, "get_anthropic_client", lambda: real)
    monkeypatch.setattr(cal.ai_cover_letter, "load_resume_text", lambda *a, **k: "")
    monkeypatch.setattr(
        cal.ai_fit_judge, "get_anthropic_client", cal.ai_fit_judge.get_anthropic_client
    )
    monkeypatch.setitem(cal.SPENT, "usd", 0.0)
    cal.install_shims(["claude-haiku-5-5"])
    cal.ai_fit_judge.get_anthropic_client().messages.create(model="claude-haiku-5-5")
    # 1M input tokens of Haiku 5.5 at $0.10/M.
    assert round(cal.SPENT["usd"], 4) == 0.1
