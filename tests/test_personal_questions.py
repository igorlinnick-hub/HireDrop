"""Questions only the person can answer: never guessed, asked once, remembered for every form."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routers import activity, personal, tools
from app.schemas import AnswerQuestionRequest
from modules import ai_question_answer as aq
from modules import personal_facts as pf

USER = SimpleNamespace(id="u1", email="a@b.c")


def _facts(*pairs, **extra):
    facts = []
    for q, a in pairs:
        facts, _ = pf.upsert(facts, {"question": q, "answer": a, **extra})
    return facts


# ── the live answerer (/tools/answer-question) ──────────────────────────────────────


def _answer(req, profile, model_answer="Yes"):
    calls = []

    def fake_model(question, job=None, profile=None, options=None, unattended=False):
        calls.append({"question": question, "job": job, "unattended": unattended})
        return model_answer

    with (
        patch.object(tools, "get_profile", return_value=profile),
        patch.object(tools, "_claim_ai_slot") as claim,
        patch.object(tools.usage_db, "release_today") as release,
        patch.object(tools, "answer_screener_question", side_effect=fake_model),
        patch.object(tools.screener_cache, "build_key", return_value=None),
        patch.object(tools.jobs_db, "get_job_by_id", return_value={"location": "Miami, FL"}),
        patch.object(tools.handbacks_db, "answers_for_job", return_value={}),
    ):
        out = tools.answer_question(req, USER)
    return out, calls, claim, release


def test_the_same_question_answered_before_is_answered_from_memory_for_free():
    facts = _facts(("Are you willing to relocate to Miami, FL?", "No"))
    req = AnswerQuestionRequest(
        question="Are you willing to relocate to Miami, FL? *", options=["Yes", "No"]
    )
    out, calls, claim, _ = _answer(req, {"personal_facts": facts})
    assert out["answer"] == "No" and out["from_user"] is True
    assert calls == [] and not claim.called  # no model, no quota


def test_a_circumstance_question_is_never_answered_in_the_candidates_favour():
    # The attended prompt ("answer in their favour") would say Yes; this path must not.
    req = AnswerQuestionRequest(
        question="Are you willing to relocate to Miami, FL?", options=["Yes", "No"], job_id="j1"
    )
    out, calls, _, _ = _answer(req, {"personal_facts": []}, model_answer="Yes")
    assert calls[0]["unattended"] is True
    # The job's place reaches the model (from the pool row here).
    assert calls[0]["job"]["location"] == "Miami, FL"
    assert out == {"answer": "Yes", "cached": False, "topic": "relocation"}


def test_unknown_comes_back_blank_and_says_who_to_ask_with_the_earlier_answer():
    facts = _facts(("Relocation plans", "Moving to San Diego in December"))
    req = AnswerQuestionRequest(
        question="Are you willing to relocate to Miami, FL?", options=["Yes", "No"]
    )
    out, _, _, release = _answer(req, {"personal_facts": facts}, model_answer="")
    assert out["answer"] == ""
    ask = out["ask_person"]
    assert ask["topic"] == "relocation"
    assert [f["answer"] for f in ask["related"]] == ["Moving to San Diego in December"]
    release.assert_called_once()  # a blank answer gives its quota slot back


def test_other_questions_keep_the_normal_path():
    req = AnswerQuestionRequest(question="Years of experience with Python?", options=["0-2", "3+"])
    out, calls, _, _ = _answer(req, {"personal_facts": []}, model_answer="3+")
    assert calls[0]["unattended"] is False
    assert out["answer"] == "3+"


def test_facts_reach_the_model_once_in_the_right_block():
    facts = _facts(("Relocation plans", "Moving to San Diego in December"))
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(text="UNKNOWN")])
    with (
        patch.object(aq, "ANTHROPIC_API_KEY", "k"),
        patch.object(aq, "get_anthropic_client", return_value=client),
        patch.object(aq, "resume_text_for", return_value="Marketing."),
    ):
        out = aq.answer_screener_question(
            "Are you willing to relocate to Miami?",
            job={"title": "t", "company": "c", "location": "Miami, FL"},
            profile={"personal_facts": facts, "city": "Honolulu", "state": "HI"},
            options=["Yes", "No"],
            unattended=True,
        )
    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    system = client.messages.create.call_args.kwargs["system"]
    assert out == ""
    assert "Job Location: Miami, FL" in prompt
    assert prompt.count("Moving to San Diego in December") == 1
    assert prompt.index("FACTS ON FILE") < prompt.index("Moving to San Diego")
    assert "says nothing" in system and "Miami" in system


# ── what is waiting on the person ────────────────────────────────────────────────────


def _hb(hid, questions, answers=None, job_id=None, created="2026-10-08T10:00:00"):
    return {
        "id": hid,
        "job_title": f"Job {hid}",
        "company": f"Co {hid}",
        "platform": "greenhouse",
        "questions": questions,
        "answers": answers or {},
        "job_id": job_id,
        "created_at": created,
    }


RELOC = {"label": "Are you willing to relocate to Miami, FL?", "options": ["Yes", "No"]}
ESSAY = {"label": "Why do you want to work at Acme?", "options": []}
LICENSE = {"label": "Do you have a valid driver's license?", "options": ["Yes", "No"]}


def test_one_entry_per_question_however_many_jobs_ask_it_most_blocking_first():
    rows = [
        _hb("a", [LICENSE], created="2026-10-08T12:00:00"),
        _hb("b", [RELOC, ESSAY]),
        _hb("c", [RELOC]),
    ]
    out = personal._waiting_questions(rows, _facts(("Relocation plans", "San Diego")))
    assert [q["question"] for q in out] == [RELOC["label"], LICENSE["label"]]
    assert [j["handback_id"] for j in out[0]["jobs"]] == ["b", "c"]
    assert out[0]["topic"] == "relocation" and out[0]["related"][0]["answer"] == "San Diego"
    # The essay about one employer stays with its job in History.
    assert all("Acme" not in q["question"] for q in out)


def test_a_question_memory_already_answers_is_not_asked_again():
    rows = [_hb("a", [RELOC])]
    assert personal._waiting_questions(rows, _facts((RELOC["label"], "No"))) == []


def test_answering_requeues_the_jobs_it_completes_and_parks_the_rest():
    rows = [
        _hb("done", [RELOC], job_id="j1"),
        _hb("partial", [RELOC, ESSAY], job_id="j2"),
        _hb("other", [LICENSE]),
    ]
    facts, fact = pf.upsert([], {"question": RELOC["label"], "answer": "no"})
    with (
        patch.object(personal.handbacks_db, "list_open", return_value=rows),
        patch.object(personal.handbacks_db, "save_answers") as requeue,
        patch.object(personal.handbacks_db, "store_answers") as park,
        patch.object(personal.jobs_db, "update_job_status") as status,
    ):
        out = personal.apply_to_handbacks("u1", fact, facts)
    assert out == {"requeued": 1, "still_waiting": 1}
    requeue.assert_called_once_with("u1", "done", {RELOC["label"]: "No"})  # snapped to the option
    status.assert_called_once_with("u1", "j1", "approved")
    park.assert_called_once_with("u1", "partial", {RELOC["label"]: "No"})


def test_a_choice_question_must_be_answered_with_a_choice():
    rows = [_hb("a", [RELOC])]
    body = personal.FactBody(question=RELOC["label"], answer="Only to San Diego")
    with (
        patch.object(personal.handbacks_db, "list_open", return_value=rows),
        patch.object(personal.facts_db, "get", return_value=[]),
        patch.object(personal.facts_db, "upsert") as upsert,
    ):
        with pytest.raises(HTTPException) as e:
            personal.remember("u1", body)
    assert e.value.status_code == 400 and e.value.detail["options"] == ["Yes", "No"]
    upsert.assert_not_called()


def test_remember_saves_replaces_and_applies():
    old = _facts(("Relocation plans", "San Diego"))
    body = personal.FactBody(
        question=RELOC["label"], answer="Yes", replace_ids=[old[0]["id"]], source="popup"
    )
    saved = {}

    def fake_save(uid, facts):
        saved["facts"] = facts
        return facts

    with (
        patch.object(personal.handbacks_db, "list_open", return_value=[_hb("a", [RELOC])]),
        patch("app.db.personal_facts.get", return_value=old),
        patch("app.db.personal_facts.save", side_effect=fake_save),
        patch.object(personal.handbacks_db, "save_answers"),
        patch.object(personal.jobs_db, "update_job_status"),
    ):
        out = personal.remember("u1", body)
    assert [f["question"] for f in saved["facts"]] == [RELOC["label"]]  # San Diego replaced
    assert out["fact"]["source"] == "popup" and out["requeued"] == 1


def test_before_the_migration_saving_says_so_instead_of_pretending():
    from postgrest.exceptions import APIError

    from app.db import personal_facts as facts_db

    client = MagicMock()
    client.table.return_value.update.return_value.eq.return_value.execute.side_effect = APIError(
        {"code": "PGRST204", "message": "column not found"}
    )
    client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.side_effect = APIError(
        {"code": "42703", "message": "column does not exist"}
    )
    with patch("app.db.personal_facts.get_supabase", return_value=client):
        assert facts_db.get("u1") == []  # reads as "no facts yet"
        with pytest.raises(facts_db.FactsUnavailableError):
            facts_db.save("u1", _facts(("q", "a")))


# ── History answers are remembered too ───────────────────────────────────────────────


def test_an_answer_given_in_history_is_remembered_when_it_can_be_reused():
    saved = {}
    with (
        patch("app.db.personal_facts.get", return_value=[]),
        patch("app.db.personal_facts.save", side_effect=lambda uid, f: saved.setdefault("f", f)),
    ):
        activity._remember_reusable(
            "u1",
            [RELOC, ESSAY, LICENSE],
            {RELOC["label"]: "No", ESSAY["label"]: "I love Acme.", LICENSE["label"]: "Yes"},
        )
    questions = sorted(f["question"] for f in saved["f"])
    assert questions == sorted([RELOC["label"], LICENSE["label"]])
    assert all(f["source"] == "history" for f in saved["f"])
