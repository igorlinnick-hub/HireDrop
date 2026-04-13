import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from datetime import datetime

from app.deps import get_current_user
from app.schemas import CoverLetterRequest, LetterPreviewRequest, TemplateRequest
from database.db import get_all_jobs, get_connection
from modules.ai_cover_letter import generate_cover_letter
from modules.telegram_bot import send_notification

router = APIRouter(tags=["tools"])

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
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


def _get_today():
    return datetime.now().strftime("%Y-%m-%d")


@router.get("/stats")
def stats(user=Depends(get_current_user)):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM jobs")
    total_jobs = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM applications")
    total_applications = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM jobs WHERE date_found LIKE ?", (_get_today() + "%",))
    new_today = cursor.fetchone()[0]
    conn.close()
    return {"total_jobs": total_jobs, "total_applications": total_applications, "new_today": new_today}


@router.get("/checklist")
def checklist(user=Depends(get_current_user)):
    from data_helpers import load_profile
    profile = load_profile()
    resume_path = os.path.join(DATA_DIR, "resume.pdf")
    has_resume = os.path.exists(resume_path)
    has_keywords = len(profile.get("keywords", [])) > 0
    has_platforms = len(profile.get("platforms", [])) > 0
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM jobs")
    has_searched = cursor.fetchone()[0] > 0
    conn.close()
    return {
        "resume": has_resume,
        "keywords": has_keywords,
        "platform": has_platforms,
        "search": has_searched,
        "complete": has_resume and has_keywords and has_searched,
    }


@router.post("/tools/cover-letter")
def cover_letter(req: CoverLetterRequest, user=Depends(get_current_user)):
    jobs = get_all_jobs()
    job = next((j for j in jobs if str(j["id"]) == str(req.job_id)), None)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    letter = generate_cover_letter({
        "title": job["title"],
        "company": job["company"],
        "description": job["description"] if "description" in job.keys() else "",
    })
    return {"letter": letter, "job_title": job["title"], "company": job["company"]}


@router.post("/tools/cover-letter-preview")
def cover_letter_preview(req: LetterPreviewRequest, user=Depends(get_current_user)):
    from data_helpers import load_profile
    profile = load_profile()
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
    if responses:
        for r in responses:
            send_notification(f"JobFlow Email: {r['subject']}\nFrom: {r['sender']}")
    return {"configured": True, "count": len(responses), "emails": responses}


@router.get("/platform/inbox-urls")
def platform_inbox_urls(user=Depends(get_current_user)):
    from data_helpers import load_profile
    profile = load_profile()
    enabled = profile.get("platforms", [])
    return {p: PLATFORM_INBOX_URLS[p] for p in enabled if p in PLATFORM_INBOX_URLS}


@router.get("/extension/download")
def download_extension(user=Depends(get_current_user)):
    import zipfile
    import io
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
