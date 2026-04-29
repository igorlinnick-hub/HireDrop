import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.deps import get_current_user
from app.db import activity as activity_db

router = APIRouter(tags=["activity"])


class ActivityWriteRequest(BaseModel):
    message: str = Field(..., max_length=2000)
    level: str = "info"
    phase: Optional[str] = None
    trace_id: Optional[str] = None
    metadata: Optional[dict] = None


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
