import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.db import selectors as selectors_db
from app.db.subscriptions import is_admin
from app.deps import get_current_user

router = APIRouter(tags=["extension"])


@router.get("/extension/selectors/{platform}")
def get_selectors(platform: str, user=Depends(get_current_user)):
    row = selectors_db.get(platform)
    if not row:
        return JSONResponse(
            status_code=404, content={"error": f"No selectors for platform: {platform}"}
        )
    return {
        "platform": row["platform"],
        "version": row["version"],
        "selectors": row["selectors_json"],
        "updated_at": row["updated_at"],
    }


@router.patch("/extension/selectors/{platform}/{section}")
def patch_selectors_section(platform: str, section: str, payload: dict, user=Depends(get_current_user)):
    """Admin-only: patch any top-level section of platform selectors_json."""
    if not is_admin(getattr(user, "email", None)):
        return JSONResponse(status_code=403, content={"error": "Admin only"})
    row = selectors_db.get(platform)
    if not row:
        return JSONResponse(status_code=404, content={"error": f"No selectors for platform: {platform}"})
    selectors_json = row["selectors_json"]
    selectors_json[section] = payload
    selectors_db.upsert(platform, row["version"], selectors_json)
    return {"ok": True, "platform": platform, "section": section}
