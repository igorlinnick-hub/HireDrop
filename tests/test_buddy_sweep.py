"""The daily Drop scan: a problem day becomes a task for Igor in the handoff, without
anyone's words in it (the repository is public)."""

import importlib.util
from pathlib import Path
from unittest.mock import patch

from app.db import buddy_log

_spec = importlib.util.spec_from_file_location(
    "buddy_review", Path(__file__).parent.parent / "scripts" / "buddy_review.py"
)
review = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review)

DAY = "2026-10-08"
SECRET = "my ssn is 123-45-6789, why did it stop"


def _turn(ts, q, a, **m):
    return {
        "user_id": "u1",
        "level": m.pop("level", "info"),
        "timestamp": f"{DAY}T{ts}+00:00",
        "trace_id": ts,
        "metadata_json": {"question": q, "answer": a, "cost_usd": 0.01, **m},
    }


BAD_DAY = [
    _turn("10:00:00", SECRET, "I'm not sure, write to support@hiredrop.io."),
    _turn("11:00:00", "how much is it", "I don't know, sorry."),
    _turn("12:00:00", "hm", "", level="error", error="rabbit_hole"),
]


def test_a_clean_day_raises_nothing():
    s = buddy_log.summarize([_turn("10:00:00", "price?", "$39/month.")], [], [])
    assert buddy_log.alerts(s) == []


def test_alerts_name_each_problem():
    s = buddy_log.summarize(
        BAD_DAY, [{"trace_id": "11:00:00", "metadata_json": {"rating": "down"}}], []
    )
    found = " | ".join(buddy_log.alerts(s))
    assert "сломались" in found and "не знал ответа 2" in found and "👎" in found
    s = {"questions": 10, "cost_per_answer": 0.05, "limit_hitters": 2}
    found = " | ".join(buddy_log.alerts(s))
    assert "$0.050" in found and "лимит 20" in found


def test_a_problem_day_writes_one_task_without_anyones_words(tmp_path):
    handoff = tmp_path / "drop-actions.md"
    handoff.write_text("# drop-actions\n\nsome notes\n")
    with patch.object(review.buddy_log, "read", return_value=(BAD_DAY, [], [])):
        assert review.sweep(DAY, handoff) == 1
        assert review.sweep(DAY, handoff) == 1  # a re-run replaces the day, never stacks
    text = handoff.read_text()
    assert text.startswith("# drop-actions\n\nsome notes\n")
    assert text.count(f"<!-- drop-scan:{DAY} -->") == 1
    assert f"- [ ] **{DAY}** · 3 вопросов" in text
    assert f"scripts/buddy_review.py --day {DAY}" in text
    assert "123-45-6789" not in text and "support@hiredrop.io" not in text


def test_a_clean_day_leaves_the_handoff_alone(tmp_path):
    handoff = tmp_path / "drop-actions.md"
    handoff.write_text("# drop-actions\n")
    clean = [_turn("10:00:00", "price?", "$39/month.")]
    with patch.object(review.buddy_log, "read", return_value=(clean, [], [])):
        assert review.sweep(DAY, handoff) == 0
    assert handoff.read_text() == "# drop-actions\n"


def test_an_unreadable_log_is_a_breakage_not_all_clear(tmp_path):
    handoff = tmp_path / "drop-actions.md"
    handoff.write_text("# drop-actions\n")
    with patch.object(review.buddy_log, "read", side_effect=RuntimeError("no key")):
        assert review.sweep(DAY, handoff) == 2


def test_entries_newest_first_two_weeks_kept_text_after_the_section_kept():
    text = f"# h\n\n{review.SECTION}\n\n## Next section\n\nkeep me\n"
    for d in range(1, 18):
        day = f"2026-10-{d:02d}"
        text = review.upsert_task(
            text, day, f"<!-- drop-scan:{day} -->\n- {day}\n<!-- /drop-scan:{day} -->"
        )
    days = [line[2:] for line in text.splitlines() if line.startswith("- 2026")]
    assert days == [f"2026-10-{d:02d}" for d in range(17, 3, -1)]
    assert "## Next section\n\nkeep me" in text
