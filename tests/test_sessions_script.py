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
    assert "забрал" in capsys.readouterr().out


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
