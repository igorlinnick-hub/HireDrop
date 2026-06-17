import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends

from app.db import applications as apps_db
from app.deps import get_current_user

router = APIRouter(tags=["email"])

# Map email classification → application status stored in DB
_EMAIL_STATUS_MAP = {
    "interview_invite": "interview",
    "rejected": "rejected",
    "received": "received",
}


@router.post("/admin/process-emails")
def process_emails(user=Depends(get_current_user)):
    """Scan the configured inbox, classify emails, and update matching application statuses."""
    from modules.email_parser import check_email_responses

    emails = check_email_responses()
    updated: list[dict] = []
    skipped: list[dict] = []

    for item in emails:
        company = item.get("company", "")
        new_status = _EMAIL_STATUS_MAP.get(item["email_status"])

        if not company or not new_status:
            skipped.append({"subject": item["subject"], "reason": "no_company_or_unclassified"})
            continue

        matches = apps_db.find_by_company_all_users(company)
        if not matches:
            skipped.append({"subject": item["subject"], "reason": f"no_application_match_for_{company}"})
            continue

        for app in matches:
            if app["status"] == new_status:
                continue
            success = apps_db.update_status(app["id"], new_status)
            if success:
                updated.append(
                    {
                        "application_id": app["id"],
                        "company": app["company"],
                        "old_status": app["status"],
                        "new_status": new_status,
                        "subject": item["subject"],
                    }
                )

    return {
        "processed": len(emails),
        "updated": len(updated),
        "skipped": len(skipped),
        "details": updated,
    }


@router.get("/email/status-updates")
def email_status_updates(user=Depends(get_current_user)):
    """Return recent applications that have a non-applied status (interview/rejected/received)."""
    history = apps_db.get_history(user.id, limit=50)
    updates = [
        app for app in history
        if app["status"] in ("interview", "rejected", "received", "interview_invite")
    ]
    return updates
