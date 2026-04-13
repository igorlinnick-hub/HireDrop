import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends
from datetime import datetime

from app.deps import get_current_user
from app.schemas import CampaignStartRequest
from database.db import get_connection

router = APIRouter(tags=["campaign"])

LIMIT_PER_PLATFORM = 50

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
CAMPAIGN_STATE_PATH = os.path.join(DATA_DIR, "campaign_state.json")


def _load_campaign_state():
    import json
    if os.path.exists(CAMPAIGN_STATE_PATH):
        with open(CAMPAIGN_STATE_PATH, "r") as f:
            return json.load(f)
    return {"running": False, "filters": {}, "started_at": None}


def _save_campaign_state(state):
    import json
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CAMPAIGN_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def _get_today():
    return datetime.now().strftime("%Y-%m-%d")


@router.get("/campaign/status")
def campaign_status(user=Depends(get_current_user)):
    from data_helpers import load_profile
    state = _load_campaign_state()
    today = _get_today()
    profile = load_profile()
    enabled_platforms = profile.get("platforms", [])

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM applications WHERE date_applied LIKE ?",
        (today + "%",),
    )
    today_count = cursor.fetchone()[0]
    cursor.execute("""
        SELECT j.platform, COUNT(*) as cnt
        FROM applications a
        JOIN jobs j ON a.job_id = j.id
        WHERE a.date_applied LIKE ?
        GROUP BY j.platform
    """, (today + "%",))
    platform_counts = {row[0]: row[1] for row in cursor.fetchall()}

    if enabled_platforms:
        placeholders = ",".join("?" for _ in enabled_platforms)
        cursor.execute(
            f"SELECT COUNT(*) FROM jobs WHERE status='new' AND platform IN ({placeholders})",
            enabled_platforms,
        )
    else:
        cursor.execute("SELECT COUNT(*) FROM jobs WHERE status='new'")
    jobs_ready = cursor.fetchone()[0]
    conn.close()

    return {
        "running": state.get("running", False),
        "filters": state.get("filters", {}),
        "started_at": state.get("started_at"),
        "today_applications": today_count,
        "platform_counts": platform_counts,
        "limit_per_platform": LIMIT_PER_PLATFORM,
        "jobs_ready": jobs_ready,
    }


@router.post("/campaign/start")
def campaign_start(req: CampaignStartRequest, user=Depends(get_current_user)):
    state = {
        "running": True,
        "filters": {
            "keywords": req.keywords,
            "platforms": req.platforms,
            "location": req.location,
            "job_type": req.job_type,
        },
        "started_at": datetime.now().isoformat(),
    }
    _save_campaign_state(state)
    return {"started": True, "state": state}


@router.post("/campaign/stop")
def campaign_stop(user=Depends(get_current_user)):
    state = _load_campaign_state()
    state["running"] = False
    _save_campaign_state(state)
    return {"stopped": True}
