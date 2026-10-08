"""Drop, the support chat — POST /buddy/ask streams NDJSON events from modules/buddy.ask.

Events (one JSON object per line):
  {"type":"state","state":"thinking"|"checking"|"speaking"}   — drives the character
  {"type":"text","text":"…"}                                  — answer, streamed
  {"type":"proposal","proposal":{…}}                          — a card with one button
                                                                (modules/buddy_actions.py)
  {"type":"done","turn_id":"…"} | {"type":"error","message":"…","turn_id":"…"}

Every question is written to activity_log (phase "buddy", text in metadata, not in the
message — the run report categorises messages by keywords, and a question mentioning
"captcha" must not count as one). That row is also the daily quota counter, shared by
both uvicorn workers without a new table — and, with the answer, tools, cards, cost and
latency in it, the record the monitoring reads (app/db/buddy_log.py, the admin board's
"Drop" section, scripts/buddy_review.py). `turn_id` (= the row's trace_id) is what
POST /buddy/feedback ties a 👍/👎 or a pressed card back to.
"""

import contextlib
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.db import activity as activity_db
from app.db import buddy_log
from app.db.subscriptions import is_admin
from app.deps import get_current_user
from modules import buddy

router = APIRouter(prefix="/buddy", tags=["buddy"])

# ~$0.012/answer on Sonnet 5.5 (measured 2026-10-01) -> worst case ~$7/month per user.
DAILY_QUESTIONS = 20
DAILY_FEEDBACK = 200  # votes and card outcomes: a ceiling, not a budget


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=buddy.MAX_QUESTION_CHARS)
    # [{"role": "user"|"assistant", "text": "…"}] — earlier turns of this chat
    history: list[dict] = Field(default_factory=list, max_length=40)
    tz: str | None = Field(default=None, max_length=64)  # browser IANA zone, e.g. America/New_York
    # Something the chat did besides the text — a fixed key, mapped to a fixed note
    # (buddy.ATTACHMENT_NOTES). "resume_pdf" = a new resume was just uploaded in the chat.
    attachment: Literal["resume_pdf"] | None = None


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT00:00:00+00:00")


@router.post("/ask")
def ask(body: AskBody, user=Depends(get_current_user)):
    if (
        not is_admin(getattr(user, "email", None))
        and activity_db.count_since(user.id, buddy.BUDDY_PHASE, _today()) >= DAILY_QUESTIONS
    ):
        # Counted, so the board shows how many people hit the cap (a cap too tight is a
        # product problem; a cap hit by a script is a different one).
        with contextlib.suppress(Exception):
            activity_db.write(
                user.id,
                "buddy: daily limit",
                phase=buddy_log.LIMIT_PHASE,
                metadata={"question": body.question[:300]},
            )
        raise HTTPException(
            status_code=429,
            detail="That's a lot of questions for one day — I'm back tomorrow. "
            "Anything urgent: support@hiredrop.io.",
        )

    turn_id = uuid.uuid4().hex

    def events():
        record: dict = {}
        failed = False
        started = time.monotonic()
        try:
            for ev in buddy.ask(user, body.question, body.history, body.tz, body.attachment):
                if ev["type"] == "done":
                    record = ev
                    ev = {"type": "done", "turn_id": turn_id}  # token counts stay server-side
                elif ev["type"] == "error":
                    failed = True
                    record = ev
                    ev = {"type": "error", "message": ev["message"], "turn_id": turn_id}
                yield json.dumps(ev) + "\n"
        except Exception as e:  # noqa: BLE001 — the user gets a human sentence, we get the cause
            failed = True
            record = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
            yield (
                json.dumps(
                    {
                        "type": "error",
                        "message": "Something broke on my side and I couldn't look that up. "
                        "Try again in a minute — or write to support@hiredrop.io.",
                        "turn_id": turn_id,
                    }
                )
                + "\n"
            )
        finally:
            # Logging must never break the chat.
            with contextlib.suppress(Exception):
                usage = record.get("usage") or {}
                model = record.get("model") or buddy.MODEL
                activity_db.write(
                    user.id,
                    "buddy: question answered" if not failed else "buddy: question failed",
                    level="error" if failed else "info",
                    phase=buddy.BUDDY_PHASE,
                    trace_id=turn_id,
                    metadata={
                        "question": body.question[:1000],
                        "answer": (record.get("answer") or "")[:4000],
                        "attachment": body.attachment,
                        "turn_id": turn_id,
                        "model": model,
                        "tools": usage.get("tools") or [],
                        "tool_errors": usage.get("tool_errors") or 0,
                        "proposals": record.get("proposals") or [],
                        "input": usage.get("input"),
                        "output": usage.get("output"),
                        "cache_read": usage.get("cache_read"),
                        "cache_write": usage.get("cache_write"),
                        "cost_usd": buddy_log.cost_usd(usage, model),
                        "latency_ms": int((time.monotonic() - started) * 1000),
                        "history_turns": len(body.history or []),
                        **({"error": record["error"]} if record.get("error") else {}),
                        **({"error": "rabbit_hole"} if record.get("type") == "error" else {}),
                    },
                )

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class FeedbackBody(BaseModel):
    turn_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    rating: Literal["up", "down"] | None = None
    comment: str = Field("", max_length=500)
    # What happened to a card: pressed and it worked / dismissed / pressed and it failed.
    proposal_id: str | None = Field(None, pattern=r"^p_[0-9a-f]{8}$")
    proposal_result: Literal["accepted", "dismissed", "failed"] | None = None


@router.post("/feedback")
def feedback(body: FeedbackBody, user=Depends(get_current_user)):
    """👍/👎 on one answer, or the outcome of one card — append-only, read by the board."""
    if not body.rating and not body.proposal_result:
        raise HTTPException(status_code=400, detail="rating or proposal_result is required")
    if activity_db.count_since(user.id, buddy_log.FEEDBACK_PHASE, _today()) >= DAILY_FEEDBACK:
        raise HTTPException(status_code=429, detail="feedback limit reached for today")
    activity_db.write(
        user.id,
        "buddy: feedback",
        phase=buddy_log.FEEDBACK_PHASE,
        trace_id=body.turn_id,
        metadata=body.model_dump(exclude_none=True),
    )
    return {"ok": True}
