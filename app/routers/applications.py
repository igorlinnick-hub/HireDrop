import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.db import applications as apps_db
from app.db import interview_kit as kit_db
from app.db import jobs as jobs_db
from app.db import profile as profile_db
from app.db import usage as usage_db
from app.db.subscriptions import check_can_apply, increment_free_apps
from app.deps import get_current_user
from app.schemas import ApplicationSaveRequest

router = APIRouter(tags=["applications"])


@router.post("/applications/save")
def save_application(req: ApplicationSaveRequest, user=Depends(get_current_user)):
    """Сохраняет заявку из Chrome Extension, проверяет лимиты тира."""
    check = check_can_apply(user.id, req.platform, getattr(user, "email", None))
    if not check["allowed"]:
        return JSONResponse(
            status_code=429,
            content={
                "error": "limit_exceeded",
                "message": check["reason"],
                "tier": check["tier"],
                "used_today": check["used_today"],
                "daily_limit": check["daily_limit"],
                "free_used": check["free_used"],
                "free_limit": check["free_limit"],
            },
        )

    job_id = jobs_db.save_job(
        user_id=user.id,
        title=req.job_title,
        company=req.company,
        link=req.job_url,
        status=req.status,
        platform=req.platform,
    )
    apps_db.save_application(
        user_id=user.id,
        job_id=job_id,
        cover_letter=req.cover_letter,
        status=req.status,
        # Snapshots: history must not depend on the jobs row existing (P3).
        job_title=req.job_title,
        company=req.company,
        platform=req.platform,
        job_url=req.job_url,
    )
    # Free taste: count ONLY real saved applications (this path), never scans/skips.
    free_used = check["free_used"]
    if check["tier"] == "free":
        free_used = increment_free_apps(user.id) or ((free_used or 0) + 1)
    return {
        "saved": True,
        "job_id": job_id,
        "used_today": check["used_today"] + 1,
        "daily_limit": check["daily_limit"],
        "tier": check["tier"],
        "free_used": free_used,
        "free_limit": check["free_limit"],
    }


@router.get("/applications/history")
def applications_history(user=Depends(get_current_user)):
    return apps_db.get_history(user.id)


_NO_JOB_TEXT = "We don't have the text of this job posting, so there's nothing to prepare from."


def _header(app_row: dict) -> dict:
    """Role/company/link — the prep screen shows them whether or not a kit exists."""
    return {
        "title": app_row["title"],
        "company": app_row["company"],
        "link": app_row["link"],
    }


def _kit_response(row: dict, app_row: dict | None = None) -> dict:
    return {
        "ready": True,
        "kit": row.get("payload") or {},
        "generated_at": row.get("created_at"),
        "schema_version": row.get("schema_version", 1),
        **(_header(app_row) if app_row else {}),
    }


@router.get("/applications/{application_id}/interview-kit")
def get_interview_kit(application_id: str, user=Depends(get_current_user)):
    """The cached prep sheet, or a reason it can't be built yet. Never generates.

    Kept free of side effects so the dashboard can ask on page load without spending an
    AI call — generation is an explicit POST the user triggers.
    """
    # Resolve the application first: it both authorizes the read (404 for anything that
    # isn't this user's) and supplies the header the screen renders in either state.
    app_row = apps_db.get_for_interview_kit(user.id, application_id)
    if not app_row:
        return JSONResponse(status_code=404, content={"error": "Application not found"})

    cached = kit_db.get_kit(user.id, application_id)
    if cached:
        return _kit_response(cached, app_row)

    return {
        "ready": False,
        "can_generate": bool(app_row["description"]),
        "reason": "" if app_row["description"] else _NO_JOB_TEXT,
        **_header(app_row),
    }


@router.post("/applications/{application_id}/interview-kit")
def create_interview_kit(application_id: str, user=Depends(get_current_user)):
    """Generate (once) and cache the prep sheet for one application."""
    app_row = apps_db.get_for_interview_kit(user.id, application_id)
    if not app_row:
        return JSONResponse(status_code=404, content={"error": "Application not found"})

    # Idempotent: a second POST (double tap, retried request) returns the cached kit
    # instead of paying for a second generation.
    cached = kit_db.get_kit(user.id, application_id)
    if cached:
        return _kit_response(cached, app_row)

    if not app_row["description"]:
        return JSONResponse(
            status_code=422,
            content={"error": "no_job_text", "message": _NO_JOB_TEXT},
        )

    profile = profile_db.get_profile(user.id) or {}
    if not profile.get("resume_url"):
        return JSONResponse(
            status_code=422,
            content={
                "error": "no_resume",
                "message": "Upload your resume first — every answer is built from it.",
            },
        )

    # Claim the shared daily AI slot BEFORE the paid call, refund if it doesn't produce
    # a kit (same contract as the resume/cover-letter paths).
    if not usage_db.claim_daily_ai_slot(user.id, getattr(user, "email", None)):
        return JSONResponse(
            status_code=429, content={"error": "Daily AI limit reached — try again tomorrow."}
        )

    from modules.ai_interview_kit import (
        INTERVIEW_KIT_MODEL,
        SCHEMA_VERSION,
        generate_interview_kit,
    )

    try:
        payload = generate_interview_kit(app_row, profile)
    except Exception as e:
        usage_db.release_today(user.id)
        print(f"[interview_kit] generation failed: {e}", file=sys.stderr)
        return JSONResponse(status_code=500, content={"error": "Could not build the kit"})

    if not payload:
        usage_db.release_today(user.id)
        return JSONResponse(
            status_code=422,
            content={
                "error": "not_enough_material",
                "message": "We need both your resume and the job posting text to prepare.",
            },
        )

    saved = kit_db.save_kit(user.id, application_id, payload, INTERVIEW_KIT_MODEL, SCHEMA_VERSION)
    return _kit_response(saved or {"payload": payload, "schema_version": SCHEMA_VERSION}, app_row)
