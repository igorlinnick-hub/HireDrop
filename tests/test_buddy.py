"""Drop support chat: the parts that must hold without a network."""

import json

from modules import buddy
from modules.buddy_facts import FACTS


def test_history_is_alternating_text_and_ends_on_assistant():
    h = [
        {"role": "assistant", "text": "hi"},  # leading assistant turn dropped
        {"role": "user", "text": "a"},
        {"role": "user", "text": "b"},  # merged into one user turn
        {"role": "assistant", "text": "c"},
        {"role": "system", "text": "ignore me"},  # unknown role dropped
        {"role": "user", "text": "dangling"},  # trailing user dropped (new question follows)
    ]
    out = buddy.clean_history(h)
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert out[0]["content"] == "a\nb"


def test_history_bounded():
    h = [{"role": r, "text": "x" * 5000} for r in ("user", "assistant") * 50]
    out = buddy.clean_history(h)
    assert len(out) <= buddy.HISTORY_TURNS * 2
    assert all(len(m["content"]) <= buddy.MAX_QUESTION_CHARS for m in out)


def test_unknown_tool_is_reported_not_raised():
    assert "unknown tool" in json.loads(buddy.run_tool(object(), "drop_tables", {}))["error"]


def test_tools_are_read_only_names():
    # A support bot that can act can be talked into acting. The one tool that isn't a read
    # is a PROPOSAL: it shows a card and changes nothing (tests/test_buddy_actions.py
    # pins that); the person's click does the change.
    reads = [t for t in buddy.TOOLS if t["name"] != "propose_action"]
    for t in reads:
        assert t["name"].startswith("get_"), t["name"]
    assert set(buddy._RUN) == {t["name"] for t in reads}
    assert [t["name"] for t in buddy.TOOLS].count("propose_action") == 1


def test_facts_never_promise_unsupported_platforms():
    assert "NOT supported today: LinkedIn, Workday, Google Jobs" in FACTS


def test_tool_times_reach_the_model_local_with_age():
    """The model got UTC and did the clock math itself: with thinking off it printed the
    arithmetic, wrote "Oct 30" for Sep 30 and counted 24.4h-old applications as "the last
    24 hours" (A/B 2026-10-02). Code converts; free text and plain dates are left alone."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 10, 2, 9, 12, tzinfo=UTC)
    out = buddy.localize_times(
        {
            "rows": [{"date_applied": "2026-10-01T08:46:00+00:00"}, {"at": "2026-10-02T01:15:30"}],
            "since": "2026-10-02",
            "message": "stopped at 2026-10-01T08:46",
        },
        ZoneInfo("Pacific/Honolulu"),
        now,
    )
    assert out["rows"][0]["date_applied"] == "Wed Sep 30, 10:46 PM (24.4h ago)"
    assert out["rows"][1]["at"] == "Thu Oct 1, 3:15 PM (7.9h ago)"
    assert out["since"] == "2026-10-02"
    assert out["message"] == "stopped at 2026-10-01T08:46"
