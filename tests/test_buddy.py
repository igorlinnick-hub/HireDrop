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
    # A support bot that can act can be talked into acting.
    for t in buddy.TOOLS:
        assert t["name"].startswith("get_"), t["name"]
    assert set(buddy._RUN) == {t["name"] for t in buddy.TOOLS}


def test_facts_never_promise_unsupported_platforms():
    assert "NOT supported today: LinkedIn, Workday, Google Jobs" in FACTS
