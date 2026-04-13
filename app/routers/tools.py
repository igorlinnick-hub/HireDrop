import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response

from app.deps import get_current_user
from app.schemas import CoverLetterRequest, LetterPreviewRequest, TemplateRequest
from app.db import jobs as jobs_db
from app.db import applications as apps_db
from app.db.profile import get_profile
from modules.ai_cover_letter import generate_cover_letter
from modules.telegram_bot import send_notification

router = APIRouter(tags=["tools"])

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "templates")

PLATFORM_INBOX_URLS = {
    "remoteok": "https://remoteok.com/messages",
    "indeed": "https://messages.indeed.com/",
    "wellfound": "https://wellfound.com/inbox",
    "glassdoor": "https://www.glassdoor.com/member/inbox",
    "ziprecruiter": "https://www.ziprecruiter.com/candidate/messages",
    "toptal": "https://www.toptal.com/tracker",
    "hired": "https://hired.com/messages",
    "flexjobs": "https://www.flexjobs.com/MyFlexJobs",
}


@router.get("/stats")
def stats(user=Depends(get_current_user)):
    return {
        "total_jobs": jobs_db.count_jobs(user.id),
        "total_applications": apps_db.count_applications(user.id),
        "new_today": jobs_db.count_jobs_found_today(user.id),
    }


@router.get("/checklist")
def checklist(user=Depends(get_current_user)):
    profile = get_profile(user.id)
    has_resume = profile.get("resume_url") or os.path.exists(
        os.path.join(os.path.dirname(__file__), "..", "..", "data", "resume.pdf")
    )
    has_keywords = len(profile.get("keywords", [])) > 0
    has_platforms = len(profile.get("platforms", [])) > 0
    has_searched = jobs_db.count_jobs(user.id) > 0
    return {
        "resume": bool(has_resume),
        "keywords": has_keywords,
        "platform": has_platforms,
        "search": has_searched,
        "complete": bool(has_resume) and has_keywords and has_searched,
    }


@router.post("/tools/cover-letter")
def cover_letter(req: CoverLetterRequest, user=Depends(get_current_user)):
    job = jobs_db.get_job_by_id(user.id, req.job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    profile = get_profile(user.id)
    letter = generate_cover_letter(
        {"title": job["title"], "company": job["company"], "description": job.get("description", "")},
        profile,
    )
    return {"letter": letter, "job_title": job["title"], "company": job["company"]}


@router.post("/tools/cover-letter-preview")
def cover_letter_preview(req: LetterPreviewRequest, user=Depends(get_current_user)):
    profile = get_profile(user.id)
    letter = generate_cover_letter(
        {
            "title": req.keywords or "the position",
            "company": "your company",
            "description": req.job_description or f"Role related to: {req.keywords}",
        },
        profile,
    )
    return {"letter": letter}


@router.post("/tools/cover-letter-template")
def save_letter_template(req: TemplateRequest, user=Depends(get_current_user)):
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    with open(os.path.join(TEMPLATES_DIR, "cover_letter.txt"), "w") as f:
        f.write(req.template)
    return {"saved": True}


@router.get("/tools/email-check")
def email_check(user=Depends(get_current_user)):
    from config import EMAIL_ADDRESS, EMAIL_PASSWORD
    from modules.email_parser import check_email_responses

    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        return {"configured": False, "count": 0, "emails": []}
    responses = check_email_responses()
    for r in responses:
        send_notification(f"JobFlow Email: {r['subject']}\nFrom: {r['sender']}")
    return {"configured": True, "count": len(responses), "emails": responses}


@router.get("/platform/inbox-urls")
def platform_inbox_urls(user=Depends(get_current_user)):
    profile = get_profile(user.id)
    enabled = profile.get("platforms", [])
    return {p: PLATFORM_INBOX_URLS[p] for p in enabled if p in PLATFORM_INBOX_URLS}


@router.get("/extension/download")
def download_extension(user=Depends(get_current_user)):
    import zipfile, io
    ext_dir = os.path.join(os.path.dirname(__file__), "..", "..", "chrome-extension")
    if not os.path.isdir(ext_dir):
        return JSONResponse(status_code=404, content={"error": "Extension folder not found"})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(ext_dir):
            for fname in files:
                full = os.path.join(root, fname)
                arcname = os.path.join("jobflow-extension", os.path.relpath(full, ext_dir))
                zf.write(full, arcname)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=jobflow-extension.zip"},
    )
