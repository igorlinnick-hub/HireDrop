import contextlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.db import activity as activity_db
from app.db import campaign as campaign_db
from app.db import handbacks as handbacks_db
from app.db import jobs as jobs_db
from app.db.subscriptions import is_admin
from app.deps import get_current_user

router = APIRouter(tags=["activity"])


class ActivityWriteRequest(BaseModel):
    message: str = Field(..., max_length=2000)
    level: str = "info"
    phase: str | None = None
    trace_id: str | None = None
    metadata: dict | None = None


@router.post("/activity")
def write_activity(req: ActivityWriteRequest, user=Depends(get_current_user)):
    activity_id = activity_db.write(
        user_id=user.id,
        message=req.message,
        level=req.level,
        phase=req.phase,
        trace_id=req.trace_id,
        metadata=req.metadata,
    )
    # A campaign-phase line IS a heartbeat, and a better one than the extension's alarm.
    # The alarm is an MV3 chrome.alarms tick: Chrome throttles it hard when the machine
    # goes idle, so a perfectly healthy run stopped refreshing last_ping_at, blew the
    # 150s TTL, and got reaped as a zombie — mid-application, twice, live on 08-15
    # (⏹ "Stopped by the server" arrived 8 seconds after a successful form step).
    # These lines are written BY the content script and ONLY while a campaign is running,
    # so they prove the CAMPAIGN is alive — the property ZOMBIE_FIX_PLAN demands — rather
    # than merely that the extension is loaded.
    if req.phase == "extension":
        with contextlib.suppress(Exception):
            if campaign_db.get_state(user.id).get("running"):
                campaign_db.touch_ping(user.id)
    return {"id": activity_id}


@router.get("/activity")
def list_activity(user=Depends(get_current_user), limit: int = 100):
    return activity_db.list_recent(user.id, limit=min(limit, 500))


@router.get("/activity/summary")
def activity_summary(
    user=Depends(get_current_user), window_hours: int = 24, since: str | None = None
):
    """Health snapshot (ROADMAP_E2E.md P3): counts of applied / fit-skips / resume-fails /
    auth-401s + errors over a window, so silent failures surface on the dashboard.

    Pass `since` (ISO ts, e.g. campaign started_at) to scope the counts to the CURRENT run
    instead of a rolling 24h window — keeps a prior run's cross-platform noise out of the chips."""
    return activity_db.summary(user.id, window_hours=min(max(window_hours, 1), 168), since=since)


class HandbackBody(BaseModel):
    job_title: str = ""
    company: str = ""
    url: str = ""
    platform: str = ""
    reason: str = ""
    steps_done: int = 0
    # What the form asked and the filler left blank. Accepts bare labels or
    # {label, options} — see handbacks_db._clean_questions. The extension has always
    # collected this; until 09-21 the POST body had nowhere to put it, so the durable
    # to-do row lost the one part the human needs in order to act.
    questions: list = []
    # The pool row, so an answer can send the job back to the approved queue.
    job_id: str | None = None


class HandbackAnswersBody(BaseModel):
    # {"<question label>": "<answer>"} — keyed by the label the form showed, which is
    # what the filler matches against on the retry.
    answers: dict[str, str]


@router.get("/handbacks")
def list_handbacks(user=Depends(get_current_user), limit: int = 20):
    """This user's unfinished applications — the ones waiting on their hands.

    Read by BOTH the extension popup and the dashboard rail badge. One source on
    purpose: two surfaces showing different counts for the same to-do is worse than
    showing none (the "one number, one owner" rule the caps already follow).
    """
    return {"handbacks": handbacks_db.list_open(user.id, limit=limit)}


@router.post("/handbacks")
def add_handback(body: HandbackBody, user=Depends(get_current_user)):
    """The extension reports a job it could not finish. Idempotent per URL."""
    row = handbacks_db.add(user.id, body.model_dump())
    return {"ok": True, "handback": row}


@router.post("/handbacks/{handback_id}/answers")
def answer_handback(handback_id: str, body: HandbackAnswersBody, user=Depends(get_current_user)):
    """The human answers the questions that stopped the application, and it goes back
    into the queue (Igor, 09-21: "кнопка прогнать заявку и заявка переходит в тап
    очередь").

    Three things happen, in this order, and the order matters:
      1. the answers are stored on the hand-back row — the filler reads them on the
         retry BEFORE any model call, so a question a human answered by hand is never
         re-derived and never answered differently the second time;
      2. the job goes back to `approved`. ATS_JOB_FAILED had flipped it to `skipped`
         so the walk would stop offering it; approved is what the queue is built from
         (GET /campaign/queue), and ext 1.8.6 puts approved rows at the HEAD of the
         next run in both modes. So "answer it" really does mean "it runs next".
      3. the row stays OPEN. A re-queued application is still unfinished — draining it
         here would hide it from the list that is tracking it. `requeued_at` is what
         the UI reads to say "back in the queue" instead of leaving the row looking
         untouched.

    Without a job_id (a native Indeed/ZR hand-back that was never a pool row) the
    answers are still stored, and the response says re-queueing didn't happen — the
    caller must not claim it did.
    """
    row = handbacks_db.get_open(user.id, handback_id)
    if not row:
        # Same answer for a wrong id and someone else's id: never confirm the row exists.
        raise HTTPException(status_code=404, detail="not_found")

    saved = handbacks_db.save_answers(user.id, handback_id, body.answers)
    if not saved:
        raise HTTPException(status_code=400, detail="no_answers")

    requeued = False
    job_id = row.get("job_id")
    if job_id:
        requeued = bool(jobs_db.update_job_status(user.id, job_id, "approved"))

    return {"ok": True, "requeued": requeued, "handback": saved}


@router.post("/handbacks/{handback_id}/resolve")
def resolve_handback(handback_id: str, user=Depends(get_current_user)):
    """The user says they finished it themselves — drain it from both surfaces.

    We do NOT verify the submit happened: we cannot see the employer's side, and
    claiming otherwise would be the kind of number this project keeps removing. This
    is the user's own checkbox, and it is described that way in the UI.
    """
    return {"ok": handbacks_db.resolve(user.id, handback_id)}


@router.get("/activity/handbacks")
def handback_stats(user=Depends(get_current_user), window_hours: int = 168):
    """Admin-only: fleet-wide view of what the auto-filler couldn't submit — top
    blocking fields, platforms, and the users hitting it most.

    This is the fix backlog, not a user-facing surface: users see the FACT that a job
    wasn't submitted (with a link to finish it) and nothing else. 403 for everyone
    else — it reads other users' rows by design."""
    if not is_admin(getattr(user, "email", None)):
        return JSONResponse(status_code=403, content={"error": "Admin only"})
    return activity_db.handback_stats(window_hours=min(max(window_hours, 1), 720))
