"""How people use Drop and where it falls short — one formula for the admin board and the
script (scripts/buddy_review.py), so a number in one is the number in the other.

What is logged (app/routers/buddy.py), all in activity_log:
  phase "buddy"           one row per question: question, answer, tools, cards shown,
                          tokens, $ cost, latency; trace_id = the turn id
  phase "buddy_feedback"  👍/👎 on an answer, and what happened to a card (pressed /
                          dismissed / failed) — keyed by the turn id
  phase "buddy_limit"     a question refused by the daily cap

Nothing here judges answers with a model: every flag is a plain rule over what was logged,
so the board costs nothing to open and a flag can always be explained.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime

from app.db.client import fetch_paged, get_supabase

PHASE = "buddy"
FEEDBACK_PHASE = "buddy_feedback"
LIMIT_PHASE = "buddy_limit"

# $ per million tokens: input, output, cache read, cache write (5 min). Anthropic list
# prices (claude-api reference, 2026-10-06). input_tokens in usage EXCLUDES cache reads
# and writes, so the four add up.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
}

# An answer that points to support or admits not knowing: Drop's honest "I can't help
# here" — fine once, a gap in its tools or facts when it repeats.
_DONT_KNOW = re.compile(
    r"support@hiredrop\.io|\bi(?:'m| am) not sure\b|\bi don'?t know\b|\bi can'?t (?:see|tell|find)\b",
    re.I,
)
LONG_ANSWER_CHARS = 1200  # the style asks for 2-4 sentences; past this it dumped
ASKED_AGAIN_SECS = 180  # a re-ask this soon means the first answer didn't land
_WORD = re.compile(r"[a-zа-яё0-9]+", re.I)


def cost_usd(usage: dict | None, model: str | None) -> float | None:
    """Dollars for one turn's token counts, or None for a model we have no price for."""
    price = PRICES.get(str(model or ""))
    if not usage or not price:
        return None
    p_in, p_out, p_read, p_write = price
    return round(
        (
            (usage.get("input") or 0) * p_in
            + (usage.get("output") or 0) * p_out
            + (usage.get("cache_read") or 0) * p_read
            + (usage.get("cache_write") or 0) * p_write
        )
        / 1e6,
        6,
    )


def _rows(phase: str, since_iso: str, until_iso: str | None, user_id: str | None, cap: int):
    def build(start: int, end: int):
        q = (
            get_supabase()
            .table("activity_log")
            .select("user_id, level, message, metadata_json, trace_id, timestamp")
            .eq("phase", phase)
            .gte("timestamp", since_iso)
        )
        if until_iso:
            q = q.lte("timestamp", until_iso)
        if user_id:
            q = q.eq("user_id", user_id)  # service_role: the filter is the scope
        return q.order("timestamp", desc=False).order("id").range(start, end)

    return fetch_paged(build, cap)


def read(
    since_iso: str, until_iso: str | None = None, user_id: str | None = None, cap: int = 20_000
):
    """(turns, feedback, limit_hits) rows for the window."""
    return (
        _rows(PHASE, since_iso, until_iso, user_id, cap),
        _rows(FEEDBACK_PHASE, since_iso, until_iso, user_id, cap),
        _rows(LIMIT_PHASE, since_iso, until_iso, user_id, cap),
    )


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if len(w) > 2}


def _similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.5


def _ts(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(row.get("timestamp") or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def summarize(
    turns: list[dict], feedback: list[dict], limit_hits: list[dict], flagged_limit: int = 30
) -> dict:
    """Every number the board and the script show, plus the turns worth reading."""
    votes: dict[str, str] = {}
    card_results: dict[str, str] = {}
    for f in feedback:
        m = f.get("metadata_json") or {}
        tid = f.get("trace_id") or m.get("turn_id")
        if m.get("rating") in ("up", "down") and tid:
            votes[tid] = m["rating"]
        if m.get("proposal_id") and m.get("proposal_result"):
            card_results[m["proposal_id"]] = m["proposal_result"]

    by_user: dict[str, list[dict]] = defaultdict(list)
    for t in turns:
        by_user[t.get("user_id") or "?"].append(t)

    flagged: list[dict] = []
    tools: Counter = Counter()
    kinds_shown: Counter = Counter()
    kinds_pressed: Counter = Counter()
    costs: list[float] = []
    latencies: list[float] = []
    failed = dont_know = long_answers = asked_again = cards_shown = 0
    by_day: Counter = Counter()

    for uid, rows in by_user.items():
        rows.sort(key=lambda r: str(r.get("timestamp") or ""))
        for i, t in enumerate(rows):
            m = t.get("metadata_json") or {}
            by_day[str(t.get("timestamp") or "")[:10]] += 1
            tools.update(m.get("tools") or [])
            if isinstance(m.get("cost_usd"), (int, float)):
                costs.append(float(m["cost_usd"]))
            if isinstance(m.get("latency_ms"), (int, float)):
                latencies.append(float(m["latency_ms"]))
            for p in m.get("proposals") or []:
                cards_shown += 1
                kinds_shown[p.get("kind")] += 1
                if card_results.get(p.get("id")) == "accepted":
                    kinds_pressed[p.get("kind")] += 1

            flags = []
            if t.get("level") == "error" or m.get("error"):
                failed += 1
                flags.append("failed")
            answer = m.get("answer") or ""
            if answer and _DONT_KNOW.search(answer):
                dont_know += 1
                flags.append("didn't know")
            if len(answer) > LONG_ANSWER_CHARS:
                long_answers += 1
                flags.append("too long")
            if m.get("tool_errors"):
                flags.append("lookup failed")
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            t0, t1 = _ts(t), _ts(nxt) if nxt else None
            if (
                nxt
                and t0
                and t1
                and (t1 - t0).total_seconds() <= ASKED_AGAIN_SECS
                and _similar(
                    m.get("question") or "", (nxt.get("metadata_json") or {}).get("question") or ""
                )
            ):
                asked_again += 1
                flags.append("asked again")
            vote = votes.get(t.get("trace_id") or m.get("turn_id") or "")
            if vote == "down":
                flags.append("thumbs down")
            if flags:
                flagged.append(
                    {
                        "at": t.get("timestamp"),
                        "account": (uid or "?")[:8],
                        "flags": ", ".join(flags),
                        "question": (m.get("question") or "")[:300],
                        "answer": answer[:600],
                        "tools": ", ".join(m.get("tools") or []),
                    }
                )

    n = len(turns)
    pressed = sum(1 for r in card_results.values() if r == "accepted")
    up = sum(1 for v in votes.values() if v == "up")
    down = sum(1 for v in votes.values() if v == "down")
    flagged.sort(key=lambda f: str(f["at"] or ""), reverse=True)
    return {
        "questions": n,
        "askers": len(by_user),
        "per_asker": round(n / len(by_user), 1) if by_user else 0,
        "failed": failed,
        "didnt_know": dont_know,
        "asked_again": asked_again,
        "too_long": long_answers,
        "thumbs_up": up,
        "thumbs_down": down,
        "cards_shown": cards_shown,
        "cards_pressed": pressed,
        "cards_by_kind": [
            {"kind": k, "shown": c, "pressed": kinds_pressed.get(k, 0)}
            for k, c in kinds_shown.most_common()
        ],
        "limit_hits": len(limit_hits),
        "limit_hitters": len({r.get("user_id") for r in limit_hits}),
        "cost_usd": round(sum(costs), 4),
        "cost_per_answer": round(sum(costs) / len(costs), 5) if costs else None,
        "latency_p50_s": round(_pct(latencies, 0.5) / 1000, 1) if latencies else None,
        "latency_p95_s": round(_pct(latencies, 0.95) / 1000, 1) if latencies else None,
        "tools": [{"tool": k, "calls": c} for k, c in tools.most_common()],
        "by_day": sorted(by_day.items()),
        "flagged": flagged[:flagged_limit],
        "flagged_total": len(flagged),
    }
