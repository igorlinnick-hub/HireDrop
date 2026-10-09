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
    # The lane is free; the session itself is still open, back to "no lane claimed".
    [claim] = sessions.load_claims()
    assert (claim["status"], claim["lane"], claim["goal"]) == ("open", "", "")
    assert claim["path"].parent.name == ".open"


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


# ── the standard: on the board by mechanism, not by memory ──────────────────────


def _run_hook(monkeypatch, cmd, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return sessions.main([cmd])


def test_a_session_is_on_the_board_the_moment_it_opens(monkeypatch, capsys):
    """A read-only session that never claims must still be visible."""
    _run_hook(monkeypatch, "hook", {"session_id": "s1", "source": "startup"})
    [c] = sessions.load_claims()
    assert (c["session_id"], c["status"], c["lane"]) == ("s1", "open", "")
    # An idle tab that was never asked anything is not work: not on the board yet.
    assert not any(
        sessions.name_for("s1") in line
        for line in sessions.board_lines([c], None, sessions.now_utc())
    )
    _run_hook(
        monkeypatch,
        "hook-prompt",
        {"session_id": "s1", "prompt": "Посчитай экономику ИИ за октябрь. Код не меняй."},
    )
    [c] = sessions.load_claims()
    assert c["goal"] == "авто: Посчитай экономику ИИ за октябрь. Код не меняй"
    assert c["prompts"] == "1"
    lines = sessions.board_lines([c], None, sessions.now_utc())
    assert any("[лейн не заявлен]" in line and "экономику ИИ" in line for line in lines)
    out = capsys.readouterr().out
    assert "цель пока взята из запроса" in out and "claim --session s1" in out


def test_the_first_request_registers_a_session_the_start_hook_missed(monkeypatch):
    """Opened before the hook was installed (seconds too early)."""
    _run_hook(monkeypatch, "hook-prompt", {"session_id": "late", "prompt": "fix the ZR skip"})
    [c] = sessions.load_claims()
    assert c["status"] == "open" and c["goal"] == "авто: fix the ZR skip"
    [e] = [e for e in sessions.load_events() if e["kind"] == "open"]
    assert e["source"] == "prompt"


def test_the_auto_goal_never_carries_secrets_or_pasted_blocks():
    assert (
        sessions.goal_from_prompt("<pasted_content>sk-abc</pasted_content>\nship it") == "ship it"
    )
    assert "sk-" not in sessions.goal_from_prompt("use key sk-ant-api03-AAAAAAAAAAAAAAAA now")
    assert "@" not in sessions.goal_from_prompt("write to igor.linnick@gmail.com about it")
    assert (
        "token" not in sessions.goal_from_prompt("token=ghp_abcdefghijklmnop1234 push it").lower()
    )
    assert sessions.goal_from_prompt("/model opus") == ""
    assert len(sessions.goal_from_prompt("x" * 10 + " word" * 100)) <= 120


def test_a_nudge_comes_once_at_the_third_request_not_every_turn(monkeypatch, capsys):
    for i in range(5):
        _run_hook(monkeypatch, "hook-prompt", {"session_id": "s1", "prompt": f"step {i} please"})
    out = capsys.readouterr().out
    assert out.count("цель пока взята из запроса") == 1
    assert out.count("лейн не заявлен") == 1


def test_claim_replaces_the_open_claim_and_an_auto_goal_is_not_a_goal(monkeypatch):
    _run_hook(monkeypatch, "hook", {"session_id": "s1", "source": "startup"})
    _run_hook(monkeypatch, "hook-prompt", {"session_id": "s1", "prompt": "count costs"})
    assert sessions.main(["claim", "--session", "s1", "--lane", "economics"]) == 2
    assert (
        sessions.main(
            ["claim", "--session", "s1", "--lane", "economics", "--goal", "Oct AI cost per apply"]
        )
        == 0
    )
    [c] = sessions.load_claims()
    assert (c["status"], c["lane"], c["goal"], c["prompts"]) == (
        "active",
        "economics",
        "Oct AI cost per apply",
        "1",
    )


def test_after_clear_the_open_claim_does_not_block_continuing_a_paused_lane(monkeypatch, capsys):
    sessions.main(
        ["claim", "--session", "s1", "--lane", "ext", "--goal", "ZR in History", "--now", "PR"]
    )
    _run_hook(monkeypatch, "hook-end", {"session_id": "s1"})
    _run_hook(monkeypatch, "hook", {"session_id": "s2", "source": "clear"})  # new id after /clear
    assert sessions.main(["claim", "--session", "s2", "--lane", "ext"]) == 0
    [c] = sessions.load_claims()
    assert (c["session_id"], c["goal"], c["now"]) == ("s2", "ZR in History", "PR")


def test_closing_an_undeclared_session_removes_it_instead_of_parking(monkeypatch):
    _run_hook(monkeypatch, "hook", {"session_id": "s1", "source": "startup"})
    _run_hook(monkeypatch, "hook-end", {"session_id": "s1"})
    assert sessions.load_claims() == []


def test_the_turn_hook_keeps_a_working_session_alive(monkeypatch):
    sessions.main(["claim", "--session", "s1", "--lane", "ext", "--goal", "g"])
    [c] = sessions.load_claims()
    c["updated"] = (sessions.now_utc() - timedelta(hours=9)).isoformat()
    sessions.write_claim(c)
    assert sessions.is_stale(sessions.load_claims()[0], sessions.now_utc())
    _run_hook(monkeypatch, "hook-beat", {"session_id": "s1"})
    assert not sessions.is_stale(sessions.load_claims()[0], sessions.now_utc())


def test_hooks_never_fail_a_request_on_garbage(monkeypatch):
    for cmd in ("hook-prompt", "hook-beat", "hook-end"):
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert sessions.main([cmd]) == 0


def test_an_open_claim_gone_quiet_is_dropped_not_shown_stale(monkeypatch):
    _run_hook(monkeypatch, "hook-prompt", {"session_id": "old", "prompt": "something"})
    [c] = sessions.load_claims()
    c["updated"] = (sessions.now_utc() - timedelta(hours=9)).isoformat()
    sessions.write_claim(c)
    assert not any(
        "STALE" in line
        for line in sessions.board_lines(sessions.load_claims(), None, sessions.now_utc())
    )
    _run_hook(monkeypatch, "hook", {"session_id": "new", "source": "startup"})
    assert [c["session_id"] for c in sessions.load_claims()] == ["new"]


# ── stats: the standard's targets, measured ─────────────────────────────────────


def _ev(kind, sid, minutes_ago=10, **extra):
    at = (sessions.now_utc() - timedelta(minutes=minutes_ago)).isoformat()
    return {"at": at, "kind": kind, "sid": sid, "name": sessions.name_for(sid), **extra}


def test_stats_measures_each_target():
    events = [
        # a: opened, worked 4 requests, claimed on the 2nd after 6 min, paused with a handoff
        _ev("open", "a", 60, source="startup"),
        _ev("prompt1", "a", 59),
        _ev("claim", "a", 54, first=True, prompts=2, overlaps=1),
        _ev("pause", "a", 5, handoff=True, prompts=4),
        # b: worked 5 requests, never claimed, closed
        _ev("open", "b", 50, source="startup"),
        _ev("prompt1", "b", 49),
        _ev("close", "b", 3, prompts=5),
        # c: registered late (the start hook missed it), a quick question, no claim expected
        _ev("open", "c", 30, source="prompt"),
        _ev("prompt1", "c", 30),
        # d: a claimed lane that died and was taken over
        _ev("stale_takeover", "d", 20, lane="ext"),
        _ev("done", "e", 2, handoff=False, prompts=7),
    ]
    # Claude's own transcripts saw a, b, c — and "x", a session whose hooks never ran.
    seen = {"a", "b", "c", "x"}
    text = "\n".join(sessions.stats_lines(events, [], sessions.now_utc(), 7, seen))
    assert "Видимость: 3 из 4 активных" in text and "❌" in text.splitlines()[1]
    assert sessions.name_for("x") in text and "1 встали только по первому запросу" in text
    assert "Лейн заявлен: 1 из 2 сессий" in text and "50%" in text and "❌" in text
    assert "на 2-м запросе, через 6 мин" in text
    assert "Хендофф при закрытии лейна: 1 из 2" in text
    assert "Тихие смерти (лейн протух без паузы): 1" in text
    assert "Пересечения файлов при заявке: 1" in text


def test_stats_without_data_says_so_instead_of_zeros():
    text = "\n".join(sessions.stats_lines([], [], sessions.now_utc(), 7))
    assert "данных нет" in text


def test_a_claim_made_with_a_short_id_is_found_by_the_hooks(monkeypatch):
    """ivory-ibis claimed with --session 158b15fc; the hooks pass the full uuid."""
    sessions.main(["claim", "--session", "158b15fc", "--lane", "bugs", "--goal", "g"])
    full = "158b15fc-5959-4ef6-a7c2-1c1e97889051"
    _run_hook(monkeypatch, "hook-prompt", {"session_id": full, "prompt": "next"})
    [c] = sessions.load_claims()
    assert (c["lane"], c["prompts"]) == ("bugs", "1")
    assert not sessions.same_session("1234567", "1234567-rest")  # too short to trust a prefix


def test_visibility_passes_when_every_active_session_was_on_the_board():
    events = [_ev("open", "a", 5, source="startup"), _ev("prompt1", "a", 4)]
    text = "\n".join(sessions.stats_lines(events, [], sessions.now_utc(), 7, {"a"}))
    assert "Видимость: 1 из 1 активных" in text and "✅" in text.splitlines()[1]


def test_transcripts_are_the_independent_witness(tmp_path, monkeypatch):
    """Visibility is checked against Claude's own transcripts, which the board never writes."""
    monkeypatch.setattr(sessions, "ROOT", Path("/Users/me/Code/JobFlow/jobflow"))
    proj = tmp_path / "-Users-me-Code-JobFlow"
    proj.mkdir()
    now = sessions.now_utc()
    (proj / "abc.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": now.isoformat().replace("+00:00", "Z")}) + "\n"
    )
    (proj / "old.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": "2020-01-01T00:00:00Z"}) + "\n"
    )
    seen = sessions.transcript_sessions(now - timedelta(hours=1), projects=tmp_path)
    assert seen == {"abc"}


def test_two_sessions_with_the_same_name_never_share_a_file(monkeypatch):
    """2,162 names: collisions happen. A review simulated a /clear-paused lane being
    overwritten by an unrelated session that hashed to the same name."""
    a, b = "sid-a-0001", "sid-b-0002"
    monkeypatch.setattr(sessions, "name_for", lambda sid: "warm-toucan")
    sessions.main(["claim", "--session", a, "--lane", "ext", "--goal", "ZR in History"])
    _run_hook(monkeypatch, "hook-end", {"session_id": a})
    _run_hook(monkeypatch, "hook", {"session_id": b, "source": "startup"})
    _run_hook(monkeypatch, "hook-end", {"session_id": b})
    [parked] = sessions.load_claims()
    assert (parked["status"], parked["lane"], parked["goal"]) == ("paused", "ext", "ZR in History")


def test_switching_lanes_parks_the_old_one_and_never_copies_its_goal(capsys):
    sessions.main(
        [
            "claim",
            "--session",
            "b",
            "--lane",
            "web",
            "--goal",
            "Settings card saves",
            "--handoff",
            "web.md",
            "--branch",
            "feat/web",
        ]
    )
    sessions.main(
        [
            "claim",
            "--session",
            "a",
            "--lane",
            "ext",
            "--goal",
            "ZR in History",
            "--handoff",
            "ext.md",
        ]
    )
    [b] = [c for c in sessions.load_claims() if c["lane"] == "web"]
    b["status"] = "paused"
    sessions.write_claim(b)
    assert sessions.main(["claim", "--session", "a", "--lane", "web"]) == 0
    by_lane = {c["lane"]: c for c in sessions.load_claims()}
    assert (by_lane["web"]["goal"], by_lane["web"]["handoff"], by_lane["web"]["branch"]) == (
        "Settings card saves",
        "web.md",
        "feat/web",
    )
    assert by_lane["web"]["session_id"] == "a"
    # ext is not lost: parked, continuable by anyone.
    assert by_lane["ext"]["status"] == "paused" and by_lane["ext"]["goal"] == "ZR in History"
    assert "claim --lane ext" in capsys.readouterr().out


def test_a_new_lane_after_another_needs_its_own_goal():
    sessions.main(["claim", "--session", "a", "--lane", "ext", "--goal", "ZR in History"])
    assert sessions.main(["claim", "--session", "a", "--lane", "web"]) == 2


def test_a_late_stop_after_exit_does_not_revive_a_parked_lane(monkeypatch):
    sessions.main(["claim", "--session", "s1", "--lane", "ext", "--goal", "g"])
    [c] = sessions.load_claims()
    c["updated"] = (sessions.now_utc() - timedelta(minutes=5)).isoformat()
    sessions.write_claim(c)
    _run_hook(monkeypatch, "hook-end", {"session_id": "s1"})
    _run_hook(monkeypatch, "hook-beat", {"session_id": "s1"})
    [c] = sessions.load_claims()
    assert c["status"] == "paused"


def test_a_short_id_claim_keeps_the_full_uuid_so_stats_see_one_session(monkeypatch):
    full = "158b15fc-5959-4ef6-a7c2-1c1e97889051"
    _run_hook(monkeypatch, "hook", {"session_id": full, "source": "startup"})
    for i in range(4):
        _run_hook(monkeypatch, "hook-prompt", {"session_id": full, "prompt": f"step {i} now"})
    sessions.main(["claim", "--session", "158b15fc", "--lane", "bugs", "--goal", "g"])
    [c] = sessions.load_claims()
    assert c["session_id"] == full
    text = "\n".join(
        sessions.stats_lines(
            sessions.load_events(), sessions.load_claims(), sessions.now_utc(), 7, {full}
        )
    )
    assert "Лейн заявлен: 1 из 1" in text


def test_the_nudge_fires_even_when_no_goal_could_be_read(monkeypatch, capsys):
    for _ in range(3):
        _run_hook(monkeypatch, "hook-prompt", {"session_id": "s1", "prompt": "/code-review high"})
    assert "лейн не заявлен" in capsys.readouterr().out


def test_an_open_claim_is_never_in_the_committed_folder(monkeypatch, tmp_sessions):
    _run_hook(
        monkeypatch, "hook-prompt", {"session_id": "s1", "prompt": "deploy with password hunter2"}
    )
    assert list(tmp_sessions.glob("*.md")) == []
    [c] = sessions.load_claims()
    assert c["path"].parent == tmp_sessions / ".open"
    assert "hunter2" not in c["goal"]


def test_one_unreadable_claim_does_not_blind_the_hooks(tmp_sessions):
    sessions.main(["claim", "--session", "s1", "--lane", "ext", "--goal", "g"])
    (tmp_sessions / "broken.md").write_bytes(b"\xff\xfe\x00garbage")
    assert [c["lane"] for c in sessions.load_claims()] == ["ext"]


def test_the_scrub_cuts_at_secret_words_in_both_languages():
    g = sessions.goal_from_prompt
    assert "hunter2" not in g("my password is hunter2 please fix")
    assert "Hunter2024" not in g("пароль от админки: Hunter2024!")
    assert "abcd1234efgh" not in g("токен railway abcd1234efgh")
    assert "hunter2" not in g("set db_password=hunter2 and deploy")
    assert "AKIAIOSFODNN7EXAMPLE" not in g("rotate AKIAIOSFODNN7EXAMPLE today")
    assert "808" not in g("call me at +1 808 555 0199 later")
    assert g("Посчитай экономику ИИ за октябрь") == "Посчитай экономику ИИ за октябрь"


def test_the_script_runs_on_macos_system_python():
    """Hooks call bare python3; under a minimal PATH that is /usr/bin/python3 (3.9)."""
    import shutil
    import subprocess

    py = "/usr/bin/python3"
    if not shutil.which(py):
        pytest.skip("no system python3")
    script = Path(sessions.__file__ or _SPEC.origin)
    r = subprocess.run([py, str(script), "stats", "--days", "1"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_every_request_shows_who_else_works_and_the_wait_or_work_rule(monkeypatch, capsys):
    sessions.main(
        ["claim", "--session", "runner", "--lane", "indeed", "--goal", "g", "--now", "live Indeed run",
         "--scope", "scripts/e2e/drive.py"]
    )
    sessions.main(["claim", "--session", "me", "--lane", "zr", "--goal", "g", "--scope", "scripts/e2e/"])
    capsys.readouterr()
    for _ in range(2):  # not once — on every request
        _run_hook(monkeypatch, "hook-prompt", {"session_id": "me", "prompt": "run ZR"})
        out = capsys.readouterr().out
        assert f"{sessions.name_for('runner')} [indeed]" in out and "live Indeed run" in out
        assert "общие файлы с тобой" in out
        assert "ждём | работаем" in out
        assert f"{sessions.name_for('me')} [" not in out  # never lists itself


def test_alone_on_the_board_the_request_hook_stays_quiet(monkeypatch, capsys):
    sessions.main(["claim", "--session", "me", "--lane", "zr", "--goal", "g"])
    capsys.readouterr()
    _run_hook(monkeypatch, "hook-prompt", {"session_id": "me", "prompt": "next"})
    assert capsys.readouterr().out == ""
