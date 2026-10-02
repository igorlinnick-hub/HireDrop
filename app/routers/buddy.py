"""Drop, the support chat — POST /buddy/ask streams NDJSON events from modules/buddy.ask.

Events (one JSON object per line):
  {"type":"state","state":"thinking"|"checking"|"speaking"}   — drives the character
  {"type":"text","text":"…"}                                  — answer, streamed
  {"type":"done"} | {"type":"error","message":"…"}

Every question is written to activity_log (phase "buddy", text in metadata, not in the
message — the run report categorises messages by keywords, and a question mentioning
"captcha" must not count as one). That row is also the daily quota counter, shared by
both uvicorn workers without a new table.
"""

import contextlib
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.db import activity as activity_db
from app.db.subscriptions import is_admin
from app.deps import get_current_user
from modules import buddy

router = APIRouter(prefix="/buddy", tags=["buddy"])

# ~$0.012/answer on Sonnet 5.5 (measured 2026-10-01) -> worst case ~$7/month per user.
DAILY_QUESTIONS = 20


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=buddy.MAX_QUESTION_CHARS)
    # [{"role": "user"|"assistant", "text": "…"}] — earlier turns of this chat
    history: list[dict] = Field(default_factory=list, max_length=40)
    tz: str | None = Field(default=None, max_length=64)  # browser IANA zone, e.g. America/New_York


@router.post("/ask")
def ask(body: AskBody, user=Depends(get_current_user)):
    today = datetime.now(UTC).strftime("%Y-%m-%dT00:00:00+00:00")
    if (
        not is_admin(getattr(user, "email", None))
        and activity_db.count_since(user.id, buddy.BUDDY_PHASE, today) >= DAILY_QUESTIONS
    ):
        raise HTTPException(
            status_code=429,
            detail="That's a lot of questions for one day — I'm back tomorrow. "
            "Anything urgent: support@hiredrop.io.",
        )

    def events():
        usage, failed = None, False
        try:
            for ev in buddy.ask(user, body.question, body.history, body.tz):
                if ev["type"] == "done":
                    usage = ev
                    ev = {"type": "done"}  # token counts stay server-side
                elif ev["type"] == "error":
                    failed = True
                yield json.dumps(ev) + "\n"
        except Exception as e:  # noqa: BLE001 — the user gets a human sentence, we get the cause
            failed = True
            usage = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
            yield (
                json.dumps(
                    {
                        "type": "error",
                        "message": "Something broke on my side and I couldn't look that up. "
                        "Try again in a minute — or write to support@hiredrop.io.",
                    }
                )
                + "\n"
            )
        finally:
            # Logging must never break the chat.
            with contextlib.suppress(Exception):
                activity_db.write(
                    user.id,
                    "buddy: question answered" if not failed else "buddy: question failed",
                    level="error" if failed else "info",
                    phase=buddy.BUDDY_PHASE,
                    metadata={"question": body.question[:1000], **(usage or {})},
                )

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
