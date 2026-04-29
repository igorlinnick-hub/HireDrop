import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.deps import get_current_user
from app.db import selectors as selectors_db

router = APIRouter(tags=["extension"])


@router.get("/extension/selectors/{platform}")
def get_selectors(platform: str, user=Depends(get_current_user)):
    row = selectors_db.get(platform)
    if not row:
        return JSONResponse(status_code=404, content={"error": f"No selectors for platform: {platform}"})
    return {
        "platform": row["platform"],
        "version": row["version"],
        "selectors": row["selectors_json"],
        "updated_at": row["updated_at"],
    }
