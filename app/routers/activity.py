import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.db import activity as activity_db
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
    return {"id": activity_id}


@router.get("/activity")
def list_activity(user=Depends(get_current_user), limit: int = 100):
    return activity_db.list_recent(user.id, limit=min(limit, 500))


@router.get("/activity/summary")
def activity_summary(user=Depends(get_current_user), window_hours: int = 24, since: str | None = None):
    """Health snapshot (ROADMAP_E2E.md P3): counts of applied / fit-skips / resume-fails /
    auth-401s + errors over a window, so silent failures surface on the dashboard.

    Pass `since` (ISO ts, e.g. campaign started_at) to scope the counts to the CURRENT run
    instead of a rolling 24h window — keeps a prior run's cross-platform noise out of the chips."""
    return activity_db.summary(user.id, window_hours=min(max(window_hours, 1), 168), since=since)


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
