"""Tap-review relay endpoints — the bridge that lets a PHONE approve applications.

Flow: extension (desktop) POSTs the prepared card → phone GETs it and POSTs the
decision → extension polls the same GET, sees approved/skipped, submits on the
computer. The in-browser postMessage bridge stays the primary path when the
dashboard and extension share a browser; this relay only matters when they don't.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.db import tap_review as tap_db
from app.deps import get_current_user

router = APIRouter(tags=["review"])


@router.post("/review/pending")
def publish_pending(body: dict, user=Depends(get_current_user)):
    review_id = str(body.get("id") or "").strip()
    if not review_id:
        return JSONResponse(status_code=400, content={"error": "id is required"})
    payload = {
        k: body.get(k)
        for k in ("job_title", "company", "description", "cover_letter", "summary", "job_url")
        if body.get(k)
    }
    tap_db.upsert_pending(user.id, review_id, payload)
    return {"saved": True, "id": review_id}


@router.get("/review/pending")
def get_pending(user=Depends(get_current_user)):
    """Current card + status. The phone renders status='pending'; the extension
    polls this and acts on approved/skipped."""
    row = tap_db.get_current(user.id)
    if not row:
        return {"review": None}
    return {
        "review": {
            "id": row["review_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "decided_at": row.get("decided_at"),
            **(row.get("payload") or {}),
        }
    }


@router.post("/review/decision")
def post_decision(body: dict, user=Depends(get_current_user)):
    review_id = str(body.get("id") or "").strip()
    decision = body.get("decision")
    if decision not in ("approved", "skipped"):
        return JSONResponse(status_code=400, content={"error": "decision must be approved | skipped"})
    if not review_id:
        return JSONResponse(status_code=400, content={"error": "id is required"})
    ok = tap_db.set_decision(user.id, review_id, decision)
    return {"saved": ok}
