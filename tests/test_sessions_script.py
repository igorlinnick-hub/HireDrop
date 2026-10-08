"""scripts/sessions.py — session naming, claims as leases, scope overlap, the SessionStart hook."""

import importlib.util
import io
import json
from datetime import timedelta
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "sessions_script", Path(__file__).resolve().parent.parent / "scripts" / "sessions.py"
)
sessions = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sessions)


@pytest.fixture(autouse=True)
def tmp_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(sessions, "ROOT", tmp_path)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    return tmp_path / "sessions"


def test_name_is_stable_per_session_and_differs_between_sessions():
    assert sessions.name_for("abc") == sessions.name_for("abc")
    names = {sessions.name_for(f"s{i}") for i in range(50)}
    assert len(names) > 30
    assert all("-" in n for n in names)


def test_claim_then_beat_then_done(tmp_sessions, capsys):
    assert (
        sessions.main(
            ["claim", "--session", "s1", "--lane", "ext", "--goal", "ZR in History", "--now", "PR"]
        )
        == 0
    )
    [claim] = sessions.load_claims()
    assert claim["name"] == sessions.name_for("s1")
    assert (claim["lane"], claim["goal"], claim["now"]) == ("ext", "ZR in History", "PR")

    assert sessions.main(["beat", "--session", "s1", "--now", "CI green"]) == 0
    [claim] = sessions.load_claims()
    assert claim["now"] == "CI green" and claim["goal"] == "ZR in History"

    assert sessions.main(["done", "--session", "s1"]) == 0
    assert sessions.load_claims() == []


def test_claim_without_session_id_refuses():
    assert sessions.main(["claim", "--lane", "ext", "--goal", "x"]) == 2


def test_stale_claim_on_same_lane_is_taken_over(capsys):
    sessions.main(["claim", "--session", "old", "--lane", "ext", "--goal", "old goal"])
    [old] = sessions.load_claims()
    old["updated"] = (sessions.now_utc() - timedelta(hours=sessions.STALE_HOURS + 1)).isoformat()
    sessions.write_claim(old)

    sessions.main(["claim", "--session", "new", "--lane", "ext", "--goal", "new goal"])
    claims = sessions.load_claims()
    assert [c["session_id"] for c in claims] == ["new"]
    assert "продолжаю за" in capsys.readouterr().out


def test_live_claim_on_same_lane_is_kept_and_warned(capsys):
    sessions.main(["claim", "--session", "a", "--lane", "ext", "--goal", "g"])
    sessions.main(["claim", "--session", "b", "--lane", "ext", "--goal", "g"])
    assert len(sessions.load_claims()) == 2
    assert "уже держит" in capsys.readouterr().err


def test_scope_overlap_detected_for_dir_prefix_and_glob():
    assert sessions.scopes_overlap(["chrome-extension/"], ["chrome-extension/content.js"])
    assert sessions.scopes_overlap(["app/*.py"], ["app/main.py"])
    assert not sessions.scopes_overlap(["app/db"], ["app/dbx/x.py"])
    assert not sessions.scopes_overlap(["app/"], ["modules/"])


def test_board_flags_overlap_between_live_claims():
    sessions.main(
        ["claim", "--session", "a", "--lane", "ext", "--goal", "g", "--scope", "chrome-extension/"]
    )
    sessions.main(
        [
            "claim",
            "--session",
            "b",
            "--lane",
            "web",
            "--goal",
            "g",
            "--scope",
            "chrome-extension/content.js",
        ]
    )
    lines = sessions.board_lines(sessions.load_claims(), None, sessions.now_utc())
    assert any("ПЕРЕСЕЧЕНИЕ" in line for line in lines)


def _hook(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return sessions.main(["hook"])


def test_hook_names_session_and_asks_for_claim(monkeypatch, capsys):
    assert _hook(monkeypatch, {"session_id": "s9", "source": "startup"}) == 0
    out = capsys.readouterr().out
    assert sessions.name_for("s9") in out
    assert "claim --session s9" in out


def test_hook_after_compact_reminds_own_goal(monkeypatch, capsys):
    sessions.main(["claim", "--session", "s9", "--lane", "ext", "--goal", "ZR in History"])
    capsys.readouterr()
    _hook(monkeypatch, {"session_id": "s9", "source": "compact"})
    out = capsys.readouterr().out
    assert "ЦЕЛЬ: ZR in History" in out
    assert "сверь" in out


def test_hook_without_payload_is_silent(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert sessions.main(["hook"]) == 0
    assert capsys.readouterr().out == ""


def test_clear_pauses_lane_and_next_session_continues_it(monkeypatch, capsys):
    sessions.main(
        ["claim", "--session", "s1", "--lane", "ext", "--goal", "ZR in History", "--now", "PR"]
    )
    sessions.main(["beat", "--session", "s1", "--handoff", "docs/handoff/apply-losses.md"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    assert sessions.main(["hook-end"]) == 0
    [parked] = sessions.load_claims()
    assert parked["status"] == "paused"
    lines = sessions.board_lines([parked], None, sessions.now_utc())
    assert any("ПАУЗА [ext]" in line for line in lines)

    capsys.readouterr()
    assert sessions.main(["claim", "--session", "s2", "--lane", "ext"]) == 0
    [claim] = sessions.load_claims()
    assert claim["session_id"] == "s2" and claim["status"] == "active"
    assert (claim["goal"], claim["now"], claim["handoff"]) == (
        "ZR in History",
        "PR",
        "docs/handoff/apply-losses.md",
    )
    assert "продолжаю за" in capsys.readouterr().out


def test_paused_claim_does_not_count_as_overlap():
    sessions.main(["claim", "--session", "a", "--lane", "ext", "--goal", "g", "--scope", "x/"])
    [a] = sessions.load_claims()
    a["status"] = "paused"
    sessions.write_claim(a)
    sessions.main(["claim", "--session", "b", "--lane", "web", "--goal", "g", "--scope", "x/y"])
    lines = sessions.board_lines(sessions.load_claims(), None, sessions.now_utc())
    assert not any("ПЕРЕСЕЧЕНИЕ" in line for line in lines)


def test_new_lane_needs_a_goal():
    assert sessions.main(["claim", "--session", "s1", "--lane", "new"]) == 2


def test_sign_prints_handoff_line(capsys):
    sessions.main(["claim", "--session", "s1", "--lane", "ext", "--goal", "G", "--now", "N"])
    capsys.readouterr()
    assert sessions.main(["sign", "--session", "s1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"Сессия: {sessions.name_for('s1')} · лейн ext · цель: G · шаг: N")


def test_board_shows_branch_so_another_session_can_continue():
    sessions.main(
        ["claim", "--session", "a", "--lane", "ext", "--goal", "g", "--branch", "fix/zr-truth"]
    )
    [a] = sessions.load_claims()
    lines = sessions.board_lines([a], None, sessions.now_utc())
    assert any("ветка: fix/zr-truth" in line for line in lines)
    a["status"] = "paused"
    lines = sessions.board_lines([a], None, sessions.now_utc())
    assert any("ветка: fix/zr-truth" in line for line in lines)
