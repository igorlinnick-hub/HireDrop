import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends

from app.db import applications as apps_db
from app.deps import get_current_user

router = APIRouter(tags=["email"])

# NOTE: the manual `/admin/process-emails` trigger was removed 2026-06-29.
# It was gated only by get_current_user (no admin check), so any authenticated
# user could scan the shared inbox, match applications across ALL users via
# find_by_company_all_users(), mutate their statuses, and read other users'
# company/subject data back in the response — a cross-tenant leak. The same
# matching runs automatically and safely server-side in main.py's
# _email_poll_loop every 30 min, so the endpoint was pure redundant surface.


@router.get("/email/status-updates")
def email_status_updates(user=Depends(get_current_user)):
    """Return recent applications that have a non-applied status (interview/rejected/received)."""
    history = apps_db.get_history(user.id, limit=50)
    updates = [
        app for app in history
        if app["status"] in ("interview", "rejected", "received", "interview_invite")
    ]
    return updates
