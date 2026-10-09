"""Drop proposes, the person presses — and every answer is logged so we can watch it."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.db import buddy_log
from modules import buddy, buddy_actions
from modules import personal_facts as pf

USER = SimpleNamespace(id="u1", email="a@b.c")
APP = {
    "id": "app-1",
    "title": "Designer",
    "company": "Acme",
    "date_applied": "2026-10-01T10:00:00+00:00",
    "cover_letter": "Hi Acme team…",
}


def _build(args, *, facts=(), profile=None, apps=None):
    apps = apps if apps is not None else {"app-1": APP}
    return buddy_actions.build(
        "u1",
        args,
        facts=list(facts),
        profile=profile or {},
        find_application=lambda uid, aid: apps.get(aid) if uid == "u1" else None,
    )


# ── cards are built from a fixed list, checked against the person's account ─────────


def test_remember_card_posts_to_the_facts_endpoint_and_names_what_it_replaces():
    facts, old = pf.upsert([], {"question": "Relocation plans", "answer": "Moving to San Diego"})
    card = _build(
        {
            "kind": "remember_answer",
            "question": "Relocation plans",
            "answer": "Moving to Miami in January",
            "in_letters": True,
            "replace_ids": [old["id"], "f_deadbeef"],  # the second isn't theirs: dropped
        },
        facts=facts,
    )
    step = card["steps"][0]
    assert (step["method"], step["path"]) == ("POST", "/profile/facts")
    assert step["body"]["replace_ids"] == [old["id"]]
    assert step["body"]["in_letters"] is True and step["body"]["source"] == "drop"
    assert "Replaces: Relocation plans: Moving to San Diego" in card["lines"]
    assert card["confirm"] == "Save" and card["id"].startswith("p_")


def test_the_model_cannot_name_an_endpoint():
    card = _build(
        {
            "kind": "remember_answer",
            "question": "q",
            "answer": "a",
            "path": "/campaign/stop",
            "method": "DELETE",
            "steps": [{"path": "/profile", "method": "POST"}],
        }
    )
    assert [s["path"] for s in card["steps"]] == ["/profile/facts"]


def test_someone_elses_application_gets_no_card():
    with pytest.raises(buddy_actions.ProposalError):
        _build({"kind": "open_application", "application_id": "app-of-someone-else"})
    card = _build({"kind": "open_application", "application_id": "app-1"})
    assert card["navigate"] == "/dashboard/history?app=app-1" and card["steps"] == []
    assert "Designer at Acme" in card["lines"]


def test_rebuild_needs_an_uploaded_resume_then_rebuilds_and_switches():
    with pytest.raises(buddy_actions.ProposalError):
        _build({"kind": "rebuild_ats_resume"}, profile={})
    card = _build({"kind": "rebuild_ats_resume"}, profile={"resume_url": "u1/resume.pdf"})
    assert [s["path"] for s in card["steps"]] == [
        "/profile/ats/generate",
        "/profile/resume/default",
    ]
    assert card["steps"][1]["body"] == {"choice": "ats"}


def test_use_resume_only_for_a_resume_that_exists():
    with pytest.raises(buddy_actions.ProposalError):
        _build({"kind": "use_resume", "resume": "skills"}, profile={"resume_url": "x"})
    card = _build({"kind": "use_resume", "resume": "ats"}, profile={"ats_resume_url": "u1/a.pdf"})
    assert card["steps"][0]["body"] == {"choice": "ats"}


def test_resume_city_card_shows_from_and_to():
    with pytest.raises(buddy_actions.ProposalError):
        _build({"kind": "set_resume_location", "location": "San Diego, CA"}, profile={})
    card = _build(
        {"kind": "set_resume_location", "location": "San Diego, CA"},
        profile={"ats_structure": {"contact": {"location": "Honolulu, HI"}}},
    )
    assert card["lines"] == ["From: Honolulu, HI", "To: San Diego, CA"]
    assert card["steps"][0] == {
        "method": "POST",
        "path": "/profile/ats/contact",
        "body": {"location": "San Diego, CA"},
    }


def test_forget_only_a_fact_that_exists():
    facts, f = pf.upsert([], {"question": "Weekends?", "answer": "No"})
    with pytest.raises(buddy_actions.ProposalError):
        _build({"kind": "forget_answer", "fact_id": "f_00000000"}, facts=facts)
    card = _build({"kind": "forget_answer", "fact_id": f["id"]}, facts=facts)
    assert card["steps"][0] == {
        "method": "DELETE",
        "path": f"/profile/facts/{f['id']}",
        "body": None,
    }


def test_unknown_kind_is_refused():
    with pytest.raises(buddy_actions.ProposalError):
        _build({"kind": "stop_campaign"})


def test_proposing_writes_nothing():
    """The one non-read tool changes nothing: building a card only reads."""
    with (
        patch("app.db.personal_facts.get", return_value=[]),
        patch.object(buddy, "get_profile", return_value={"resume_url": "u1/r.pdf"}),
        patch("app.db.personal_facts.save") as save,
        patch("app.db.handbacks.save_answers") as hb_save,
    ):
        content, card = buddy.run_proposal(USER, {"kind": "rebuild_ats_resume"})
    assert card and json.loads(content)["shown"] is True
    assert "Nothing has changed yet" in json.loads(content)["rule"]
    save.assert_not_called()
    hb_save.assert_not_called()


# ── the loop: a proposal becomes an event, the answer and cards reach the log ────────


class _Stream:
    def __init__(self, texts, message):
        self.text_stream = texts
        self._m = message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self._m


def _msg(stop, content, i=100, o=50):
    return SimpleNamespace(
        stop_reason=stop,
        content=content,
        usage=SimpleNamespace(
            input_tokens=i,
            output_tokens=o,
            cache_read_input_tokens=1000,
            cache_creation_input_tokens=0,
        ),
    )


def test_ask_emits_the_card_and_reports_answer_and_cards_on_done():
    tool_use = SimpleNamespace(
        type="tool_use",
        id="t1",
        name="propose_action",
        input={
            "kind": "remember_answer",
            "question": "Relocation plans",
            "answer": "San Diego, December",
            "in_letters": True,
        },
    )
    rounds = [
        _Stream([], _msg("tool_use", [tool_use])),
        _Stream(
            ["Press Save and I'll ", "use it."],
            _msg("end_turn", [SimpleNamespace(type="text", text="x")]),
        ),
    ]
    client = MagicMock()
    client.messages.stream.side_effect = lambda **kw: rounds.pop(0)
    seen = {}

    def capture(**kw):
        seen.setdefault("first", kw["messages"][-1]["content"])
        return rounds.pop(0)

    client.messages.stream.side_effect = capture
    with (
        patch.object(buddy, "get_anthropic_client", return_value=client),
        patch("app.db.personal_facts.get", return_value=[]),
        patch.object(buddy, "get_profile", return_value={}),
    ):
        events = list(
            buddy.ask(USER, "I'm moving to San Diego in December", [], None, "resume_pdf", True)
        )
    kinds = [e["type"] for e in events]
    assert "proposal" in kinds
    card = next(e for e in events if e["type"] == "proposal")["proposal"]
    assert card["kind"] == "remember_answer"
    # Reads put Drop at the desk; a card alone doesn't.
    assert not any(e.get("state") == "checking" for e in events)
    done = events[-1]
    assert done["type"] == "done" and done["answer"] == "Press Save and I'll use it."
    assert done["proposals"] == [{"id": card["id"], "kind": "remember_answer"}]
    assert "uploaded a new resume PDF" in seen["first"]


# ── monitoring ──────────────────────────────────────────────────────────────────────


def test_cost_is_tokens_times_list_price():
    usage = {"input": 1_000_000, "output": 100_000, "cache_read": 1_000_000, "cache_write": 0}
    assert buddy_log.cost_usd(usage, "claude-sonnet-5-5") == pytest.approx(2.0 + 1.0 + 0.1)
    assert buddy_log.cost_usd(usage, "some-unknown-model") is None


def _turn(uid, ts, q, a, **m):
    return {
        "user_id": uid,
        "level": m.pop("level", "info"),
        "timestamp": ts,
        "trace_id": m.get("turn_id", ts),
        "metadata_json": {"question": q, "answer": a, **m},
    }


def test_summary_flags_what_needs_reading():
    turns = [
        _turn(
            "u1",
            "2026-10-08T10:00:00+00:00",
            "why did my campaign stop today",
            "It hit the cap.",
            cost_usd=0.01,
            latency_ms=4000,
            tools=["get_campaign_status"],
            turn_id="t1",
        ),
        _turn(
            "u1",
            "2026-10-08T10:01:30+00:00",
            "why did the campaign stop today?",
            "I'm not sure — write to support@hiredrop.io.",
            cost_usd=0.02,
            latency_ms=8000,
            turn_id="t2",
            proposals=[{"id": "p_00000001", "kind": "open_application"}],
        ),
        _turn("u2", "2026-10-08T11:00:00+00:00", "price?", "x" * 1300, cost_usd=0.01, turn_id="t3"),
        _turn(
            "u2",
            "2026-10-08T12:00:00+00:00",
            "hm",
            "",
            level="error",
            error="rabbit_hole",
            turn_id="t4",
        ),
    ]
    feedback = [
        {"trace_id": "t2", "metadata_json": {"rating": "down"}},
        {"trace_id": "t1", "metadata_json": {"rating": "up"}},
        {
            "trace_id": "t2",
            "metadata_json": {"proposal_id": "p_00000001", "proposal_result": "accepted"},
        },
    ]
    s = buddy_log.summarize(turns, feedback, [{"user_id": "u3"}, {"user_id": "u3"}])
    assert s["questions"] == 4 and s["askers"] == 2
    assert (
        s["asked_again"] == 1 and s["didnt_know"] == 1 and s["too_long"] == 1 and s["failed"] == 1
    )
    assert (s["thumbs_up"], s["thumbs_down"]) == (1, 1)
    assert (s["cards_shown"], s["cards_pressed"]) == (1, 1)
    assert s["cards_by_kind"] == [{"kind": "open_application", "shown": 1, "pressed": 1}]
    assert (s["limit_hits"], s["limit_hitters"]) == (2, 1)
    assert s["cost_usd"] == pytest.approx(0.04)
    flags = {f["question"][:12]: f["flags"] for f in s["flagged"]}
    assert flags["why did my c"] == "asked again"
    assert "didn't know" in flags["why did the "] and "thumbs down" in flags["why did the "]


def test_admin_section_renders_from_the_same_summary():
    from app.routers import admin

    with patch.object(buddy_log, "read", return_value=([], [], [])):
        section = admin._section_buddy("2026-10-01T00:00:00Z", "2026-10-08T23:59:59Z")
    assert section["key"] == "buddy"
    assert {m["key"] for m in section["metrics"]} >= {"questions", "cost", "didnt_know", "cards"}
    assert [t["key"] for t in section["tables"]] == ["flagged", "cards", "tools"]


async def _collect(resp):
    return [json.loads(chunk) async for chunk in resp.body_iterator]


def test_router_logs_the_whole_turn_and_hands_out_the_turn_id():
    from app.routers import buddy as router

    def fake_ask(user, q, history, tz, attachment, cards):
        yield {"type": "state", "state": "thinking"}
        yield {"type": "proposal", "proposal": {"id": "p_00000001", "kind": "use_resume"}}
        yield {
            "type": "done",
            "model": "claude-sonnet-5-5",
            "usage": {
                "input": 1000,
                "output": 100,
                "cache_read": 0,
                "cache_write": 0,
                "tools": ["get_account"],
            },
            "answer": "Press Use it.",
            "proposals": [{"id": "p_00000001", "kind": "use_resume"}],
        }

    written = {}
    with (
        patch.object(router.activity_db, "count_since", return_value=0),
        patch.object(router, "is_admin", return_value=False),
        patch.object(router.buddy, "ask", side_effect=fake_ask),
        patch.object(router.activity_db, "write", side_effect=lambda *a, **k: written.update(k)),
    ):
        resp = router.ask(router.AskBody(question="use my ATS resume"), USER)
        lines = asyncio.run(_collect(resp))
    done = lines[-1]
    assert done["type"] == "done" and set(done) == {"type", "turn_id"}  # no token counts out
    assert any(line["type"] == "proposal" for line in lines)
    md = written["metadata"]
    assert written["trace_id"] == done["turn_id"] == md["turn_id"]
    assert md["answer"] == "Press Use it." and md["proposals"][0]["kind"] == "use_resume"
    assert md["cost_usd"] == pytest.approx(0.003)
    assert isinstance(md["latency_ms"], int)


def test_quota_refusals_are_counted_and_feedback_is_recorded():
    from fastapi import HTTPException

    from app.routers import buddy as router

    written = []
    with (
        patch.object(router.activity_db, "count_since", return_value=router.DAILY_QUESTIONS),
        patch.object(router, "is_admin", return_value=False),
        patch.object(router.activity_db, "write", side_effect=lambda *a, **k: written.append(k)),
    ):
        with pytest.raises(HTTPException) as e:
            router.ask(router.AskBody(question="again"), USER)
    assert e.value.status_code == 429 and written[0]["phase"] == buddy_log.LIMIT_PHASE

    written.clear()
    with (
        patch.object(router.activity_db, "count_since", return_value=0),
        patch.object(router.activity_db, "write", side_effect=lambda *a, **k: written.append(k)),
    ):
        router.feedback(
            router.FeedbackBody(
                turn_id="a" * 32, proposal_id="p_00000001", proposal_result="accepted"
            ),
            USER,
        )
        with pytest.raises(HTTPException):
            router.feedback(router.FeedbackBody(turn_id="a" * 32), USER)  # nothing to record
    assert written[0]["phase"] == buddy_log.FEEDBACK_PHASE and written[0]["trace_id"] == "a" * 32
    assert len(written) == 1


# ── the one-field resume edit behind "put San Diego on my resume" ────────────────────


def test_resume_city_edit_changes_only_that_line():
    from app.routers import profile

    structure = {
        "name": "Igor",
        "summary": "Marketing.",
        "contact": {"location": "Honolulu, HI", "email": "i@x.com", "phone": "", "linkedin": ""},
        "experience": [],
    }
    with (
        patch.object(profile.profile_db, "get_profile", return_value={"ats_structure": structure}),
        patch.object(profile, "_save_structure", return_value={"success": True}) as save,
    ):
        assert profile.ats_contact_location({"location": "  San Diego,   CA "}, USER) == {
            "success": True
        }
    saved = save.call_args.args[1]
    assert saved["contact"]["location"] == "San Diego, CA"
    assert saved["contact"]["email"] == "i@x.com" and saved["name"] == "Igor"

    with patch.object(profile.profile_db, "get_profile", return_value={}):
        assert profile.ats_contact_location({"location": "San Diego, CA"}, USER).status_code == 400


def test_a_chat_that_cant_draw_cards_never_gets_the_tool():
    client = MagicMock()
    tools_seen = []

    def capture(**kw):
        tools_seen.append([t["name"] for t in kw["tools"]])
        return _Stream(
            ["Go to Settings."], _msg("end_turn", [SimpleNamespace(type="text", text="x")])
        )

    client.messages.stream.side_effect = capture
    with patch.object(buddy, "get_anthropic_client", return_value=client):
        list(buddy.ask(USER, "use my ATS resume", [], None, None))
        list(buddy.ask(USER, "use my ATS resume", [], None, None, True))
    assert "propose_action" not in tools_seen[0]
    assert "propose_action" in tools_seen[1]
