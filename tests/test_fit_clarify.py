"""Drop the clarifier — what must hold (modules/fit_clarify.py).

* only close calls are asked about: |score − bar| <= BAND under the current verdict;
* two questions a day at most, the second only after a real answer; one open at a time;
* a title family is asked once — but a question offered and never opened doesn't use it up;
* the question is a fixed template: no score, no dashes, the same words every time;
* Drop records the answer only for the person's own open question, once;
* any failure means no question — never a broken dashboard or chat.
"""

import json
from collections import Counter
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from modules import buddy, fit_clarify
from scripts import clarify_report

USER = SimpleNamespace(id="u1", email="a@b.c")
V = "v1"


def _row(i, score, title="Marketing Manager", **kw):
    return {
        "id": f"job-{i}",
        "title": title,
        "company": f"Co{i}",
        "location": "San Diego, CA",
        "platform": "greenhouse",
        "link": f"https://boards.example.com/{i}",
        "status": "new",
        "fit_score": score,
        "fit_version": V,
        "fit_model": "claude-sonnet-4-6",
        "date_found": f"2026-10-0{i % 9 + 1}",
        **kw,
    }


# ── what is a family, what is a close call ───────────────────────────────────────────


def test_one_family_whatever_the_spelling():
    a = fit_clarify.category("Senior Construction Project Manager (Remote)")
    b = fit_clarify.category("Project Manager II, Construction")
    c = fit_clarify.category("construction project manager - full time")
    assert a[0] == b[0] == c[0] == "construction manager project"
    assert a[1] == "construction project manager"  # the label keeps the order people read
    assert fit_clarify.category("Project Manager")[0] != a[0]
    assert fit_clarify.category("Senior (Remote) - Full Time") == ("", "")


def test_only_close_calls_under_the_current_verdict():
    rows = [
        _row(1, 55),  # on the bar
        _row(2, 40, title="Content Writer"),  # 15 below: still in
        _row(3, 71, title="Brand Lead Designer"),  # 16 above: out
        _row(4, 50, title="Copywriter", fit_version="old-resume"),  # stale verdict
        _row(5, 50, title="SEO Specialist", status="applied"),
        _row(6, 50, title="Senior (Remote)"),  # no family to remember
    ]
    got = fit_clarify.candidates(rows, V, 55, asked=set())
    assert [r["id"] for r in got] == ["job-1", "job-2"]


def test_a_family_is_asked_once_but_an_unopened_offer_does_not_use_it_up():
    asked = [
        {"category": "manager marketing", "side": "below", "seen_at": "t"},
        {"category": "seo specialist", "side": "below"},  # offered, never opened
    ]
    rows = [_row(1, 50), _row(2, 52, title="SEO Specialist")]
    pick = fit_clarify.pick(rows, V, 55, asked)
    assert pick["id"] == "job-2"


def test_the_less_answered_side_goes_first_then_the_closest_score():
    rows = [_row(1, 54, title="Content Writer"), _row(2, 58), _row(3, 66, title="SEO Lead")]
    answered_below = [
        {"category": "x", "side": "below", "answered_at": "t"},
        {"category": "y", "side": "below", "answered_at": "t"},
    ]
    assert fit_clarify.pick(rows, V, 55, answered_below)["id"] == "job-2"  # above, 3 away
    assert fit_clarify.pick(rows, V, 55, [])["id"] == "job-1"  # 1 away wins on a tie
    assert fit_clarify.answered_sides(answered_below) == Counter(below=2)


def test_two_a_day_at_most_and_the_second_only_after_a_real_answer():
    answered = {"answered_at": "t", "skipped": False}
    assert fit_clarify.may_ask([])
    assert fit_clarify.may_ask([answered])
    assert not fit_clarify.may_ask([{"answered_at": None}])  # open: returned, not replaced
    assert not fit_clarify.may_ask([{"answered_at": "t", "skipped": True}])
    assert not fit_clarify.may_ask([answered, answered])


# ── the words the person sees ─────────────────────────────────────────────────────────


def test_the_question_is_a_template_with_no_score_and_no_dashes():
    q = fit_clarify.snapshot(_row(1, 47, title="Brand Manager — West"), 55, V)
    text = fit_clarify.question_text(q)
    assert "Brand Manager" in text and "Co1" in text and "San Diego, CA" in text
    assert "left this one off your list" in text
    assert "47" not in text and "55" not in text
    out = fit_clarify.public({**q, "id": "q1"})
    assert "fit_score" not in json.dumps(out) and "bar" not in out
    assert out["text"] == text
    above = fit_clarify.question_text({**q, "side": "above"})
    assert "made your list" in above
    for t in (text, above):
        for dash in ("—", "–", " - ", "--"):
            assert dash not in t.replace("Brand Manager — West", "")


def test_the_snapshot_keeps_what_the_measure_needs():
    q = fit_clarify.snapshot(_row(1, 60, fit_model="claude-haiku-4-5-20251001"), 55, V)
    assert (q["fit_score"], q["bar"], q["side"], q["fit_version"]) == (60, 55, "above", V)
    assert q["fit_model"].startswith("claude-haiku") and q["category"] == "manager marketing"


# ── the answer ────────────────────────────────────────────────────────────────────────


def test_an_answer_is_a_number_a_thumb_or_a_skip():
    assert fit_clarify.parse_answer({"rating": 8}) == {"rating": 8, "thumb": None, "skipped": False}
    assert fit_clarify.parse_answer({"skipped": True, "rating": 3})["skipped"] is True
    for bad in ({}, {"rating": 11}, {"rating": True}, {"rating": 6.5}, {"thumb": "sideways"}):
        with pytest.raises(ValueError):
            fit_clarify.parse_answer(bad)
    assert fit_clarify.verdict({"rating": 7}) == "fits"
    assert fit_clarify.verdict({"rating": 3, "thumb": "up"}) == "no"  # the number decides
    assert fit_clarify.verdict({"rating": 5}) == "unsure"
    assert fit_clarify.verdict({"thumb": "down"}) == "no"
    assert fit_clarify.verdict({"skipped": True}) is None


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


def _msg(stop, content):
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )
    return SimpleNamespace(stop_reason=stop, content=content, usage=usage)


def _record(i, **args):
    return SimpleNamespace(type="tool_use", id=f"t{i}", name="record_fit_answer", input=args)


def _drop(rounds, clarify, said="a 7, it's my field"):
    client = MagicMock()
    seen = []

    def stream(**kw):
        # The loop appends to one messages list: keep what this round was sent.
        seen.append({"tools": kw["tools"], "last": kw["messages"][-1]["content"]})
        return rounds.pop(0)

    client.messages.stream.side_effect = stream
    with patch.object(buddy, "get_anthropic_client", return_value=client):
        events = list(buddy.ask(USER, said, [], None, None, True, clarify=clarify))
    return events, seen


QUESTION = {
    "id": "q1",
    "title": "Marketing Manager",
    "company": "Acme",
    "location": "Austin, TX",
    "side": "below",
}


def test_drop_records_the_answer_once_on_the_persons_own_question():
    rounds = [
        _Stream([], _msg("tool_use", [_record(1, rating=7), _record(2, rating=2)])),
        _Stream(["Thanks, noted."], _msg("end_turn", [SimpleNamespace(type="text", text="x")])),
    ]
    with patch("app.db.fit_clarify.record_answer", return_value={"id": "q1"}) as save:
        events, seen = _drop(rounds, QUESTION)
    save.assert_called_once()
    args = save.call_args.args
    assert args[:3] == ("u1", "q1", {"rating": 7, "thumb": None, "skipped": False})
    assert args[3] == "a 7, it's my field"  # the person's own words, for the audit
    assert [e for e in events if e["type"] == "clarify"] == [
        {"type": "clarify", "id": "q1", "recorded": True}
    ]
    results = seen[1]["last"]
    assert json.loads(results[0]["content"])["recorded"] is True
    assert json.loads(results[1]["content"]) == {"recorded": False, "why": "already recorded"}
    # The question rides on the message, and the tool is offered only for it.
    assert "Would Marketing Manager at Acme (Austin, TX) suit you?" in seen[0]["last"]
    assert "record_fit_answer" in [t["name"] for t in seen[0]["tools"]]
    # Recording is not a lookup: Drop doesn't sit down at the desk for it.
    assert not any(e.get("state") == "checking" for e in events)


def test_without_an_open_question_there_is_no_tool_and_nothing_is_written():
    rounds = [
        _Stream([], _msg("tool_use", [_record(1, rating=9)])),
        _Stream(["Hm."], _msg("end_turn", [SimpleNamespace(type="text", text="x")])),
    ]
    with patch("app.db.fit_clarify.record_answer") as save:
        events, seen = _drop(rounds, None)
    save.assert_not_called()
    assert "record_fit_answer" not in [t["name"] for t in seen[0]["tools"]]
    assert "unknown tool" in seen[1]["last"][0]["content"]
    assert not any(e["type"] == "clarify" for e in events)


def test_an_answer_already_given_is_never_overwritten_and_a_failed_write_is_said():
    with patch("app.db.fit_clarify.record_answer", return_value=None):
        content, recorded = buddy.run_clarify_answer(USER, QUESTION, {"rating": 1}, "no")
    assert not recorded and "already answered" in json.loads(content)["why"]
    with patch("app.db.fit_clarify.record_answer", side_effect=RuntimeError("down")):
        content, recorded = buddy.run_clarify_answer(USER, QUESTION, {"rating": 1}, "no")
    assert not recorded and "could not save it" in json.loads(content)["why"]
    with patch("app.db.fit_clarify.record_answer") as save:
        content, recorded = buddy.run_clarify_answer(USER, QUESTION, {}, "hmm")
    assert not recorded and not save.called  # nothing to record: Drop asks instead


# ── the endpoint ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def router():
    from app.routers import buddy as r

    r._nothing_to_ask.clear()
    with patch.object(r, "user_day_start", return_value="2026-10-09T10:00:00+00:00"):
        yield r


OPEN_TODAY = {
    **QUESTION,
    "created_at": "2026-10-09T18:00:00+00:00",
    "answered_at": None,
    "category": "manager marketing",
}


def test_the_open_question_comes_back_all_day(router):
    with (
        patch.object(router.clarify_db, "history", return_value=[OPEN_TODAY]),
        patch.object(router, "_pick_question") as pick,
    ):
        out = router.get_clarify(USER)
    assert out["question"]["id"] == "q1" and "suit you" in out["question"]["text"]
    pick.assert_not_called()


def test_yesterdays_open_question_does_not_block_today(router):
    yesterday = {**OPEN_TODAY, "created_at": "2026-10-08T18:00:00+00:00"}
    new = {**QUESTION, "id": "q2"}
    with (
        patch.object(router.clarify_db, "history", return_value=[yesterday]),
        patch.object(router, "_pick_question", return_value=new) as pick,
    ):
        assert router.get_clarify(USER)["question"]["id"] == "q2"
    pick.assert_called_once()


def test_nothing_to_ask_is_remembered_and_a_failure_asks_nothing(router):
    with (
        patch.object(router.clarify_db, "history", return_value=[]),
        patch.object(router, "_pick_question", return_value=None) as pick,
    ):
        assert router.get_clarify(USER) == {"question": None}
        assert router.get_clarify(USER) == {"question": None}
    assert pick.call_count == 1  # the pool is not re-read on every dashboard load
    router._nothing_to_ask.clear()
    with patch.object(router.clarify_db, "history", side_effect=RuntimeError("no table")):
        assert router.get_clarify(USER) == {"question": None}


def test_a_stale_or_foreign_question_id_is_an_ordinary_message(router):
    with patch.object(router.clarify_db, "get", return_value=None):
        assert router._open_question("u1", "q-not-mine") is None
    with patch.object(router.clarify_db, "get", return_value={**OPEN_TODAY, "answered_at": "t"}):
        assert router._open_question("u1", "q1") is None
    with patch.object(router.clarify_db, "get", side_effect=RuntimeError("down")):
        assert router._open_question("u1", "q1") is None
    assert router._open_question("u1", None) is None


# ── the measure ──────────────────────────────────────────────────────────────────────


def test_the_report_counts_both_mistakes_by_model():
    haiku, sonnet = "claude-haiku-4-5-20251001", "claude-sonnet-4-6"
    rows = [
        {"side": "above", "rating": 8, "fit_model": sonnet},  # agree
        {"side": "below", "thumb": "down", "fit_model": sonnet},  # agree
        {"side": "below", "rating": 9, "fit_model": haiku},  # left off, fits
        {"side": "above", "rating": 1, "fit_model": sonnet},  # let on, doesn't
        {"side": "above", "rating": 5, "fit_model": sonnet},  # unsure
        {"side": "above", "skipped": True, "fit_model": sonnet},  # not an answer
    ]
    s = clarify_report.summarize(rows)
    assert s["all"] == {
        "answered": 5,
        "agree": 2,
        "left off, fits": 1,
        "let on, doesn't": 1,
        "unsure": 1,
    }
    assert s[haiku]["left off, fits"] == 1 and s[sonnet]["answered"] == 4
    text = clarify_report.render(s, skipped=1, people=2, days=30)
    assert "5 answered, 1 skipped, 2 people" in text and "2 (50%)" in text
