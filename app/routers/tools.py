import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.db import applications as apps_db
from app.db import jobs as jobs_db
from app.db import usage as usage_db
from app.db.profile import get_profile
from app.db.subscriptions import get_usage_summary, is_admin
from app.deps import get_current_user
from app.schemas import (
    AnswerQuestionRequest,
    AssessFitRequest,
    CoverLetterRequest,
    LetterPreviewRequest,
    TemplateRequest,
)
from config import RATE_LIMIT_ENFORCE, RATE_LIMIT_LETTERS_PER_DAY
from modules.ai_cover_letter import generate_cover_letter
from modules.ai_fit_judge import assess_fit
from modules.ai_keyword_normalize import normalize_keywords
from modules.ai_question_answer import answer_screener_question

router = APIRouter(tags=["tools"])

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "templates")


def _claim_ai_slot(user) -> None:
    """Atomically claim one slot of the daily AI quota; raise 429 when exhausted.

    The claim happens BEFORE the LLM call: the old check -> generate -> increment
    flow left a seconds-wide window (the generation itself) where parallel
    requests all passed the check and burned Anthropic spend past the cap.
    A generation that then fails must hand its slot back via
    usage_db.release_today. Admins and observe mode (RATE_LIMIT_ENFORCE=false)
    still count usage but are never blocked.
    """
    if is_admin(getattr(user, "email", None)) or not RATE_LIMIT_ENFORCE:
        usage_db.claim_today(user.id, usage_db.COUNT_ONLY_LIMIT)
        return
    if not usage_db.claim_today(user.id, RATE_LIMIT_LETTERS_PER_DAY):
        used = usage_db.get_today_count(user.id)
        raise HTTPException(
            status_code=429,
            detail=f"Daily cover letter limit reached ({used}/{RATE_LIMIT_LETTERS_PER_DAY}). Try again tomorrow.",
        )


def _ai_quota_gate(user) -> None:
    """Read-only 429 gate for endpoints that DON'T consume a slot
    (normalize-keywords is rate-limited but a correction pass shouldn't eat a
    letter slot). Racey by design — it spends nothing, so there's nothing to
    protect atomically.
    """
    if is_admin(getattr(user, "email", None)):
        return
    used = usage_db.get_today_count(user.id)
    if RATE_LIMIT_ENFORCE and used >= RATE_LIMIT_LETTERS_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=f"Daily cover letter limit reached ({used}/{RATE_LIMIT_LETTERS_PER_DAY}). Try again tomorrow.",
        )


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
    usage = get_usage_summary(user.id, getattr(user, "email", None))
    return {
        "total_jobs": jobs_db.count_jobs(user.id),
        "total_applications": apps_db.count_applications(user.id),
        "applications_today": usage["used_today"],
        "new_today": jobs_db.count_jobs_found_today(user.id),
        "tier": usage["tier"],
        "daily_limit": usage["daily_limit"],
        "remaining_today": usage["remaining_today"],
        "platform_counts": usage["platform_counts"],
        "max_per_platform": usage["max_per_platform"],
        # Free taste (FREE_TASTE_PLAN.md): dashboard renders "N / 40 free used"
        # and flips to the paywall at the limit. None for paid/admin tiers.
        "free_used": usage["free_used"],
        "free_limit": usage["free_limit"],
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
    _claim_ai_slot(user)
    try:
        letter = generate_cover_letter(
            {
                "title": job["title"],
                "company": job["company"],
                "description": job.get("description", ""),
            },
            profile,
        )
    except Exception:
        usage_db.release_today(user.id)
        raise
    return {"letter": letter, "job_title": job["title"], "company": job["company"]}


@router.post("/tools/cover-letter-preview")
def cover_letter_preview(req: LetterPreviewRequest, user=Depends(get_current_user)):
    profile = get_profile(user.id)
    _claim_ai_slot(user)
    try:
        letter = generate_cover_letter(
            {
                "title": req.keywords or "the position",
                "company": "your company",
                "description": req.job_description or f"Role related to: {req.keywords}",
            },
            profile,
        )
    except Exception:
        usage_db.release_today(user.id)
        raise
    return {"letter": letter}


# Per-user daily cap for the fit judge. It runs on every scanned job (much higher
# volume than cover letters), so it gets its own generous ceiling — well above a real
# campaign's job-scan count, but low enough to stop a script looping the raw endpoint
# to burn Anthropic spend. In-memory (resets on deploy / per worker); a determined
# abuser is still bounded per session. DB-backed counter is a follow-up if needed.
_assess_fit_counts: dict = {}
_ASSESS_FIT_DAILY_CAP = int(os.getenv("ASSESS_FIT_DAILY_CAP", "800"))


def _assess_fit_gate(user) -> None:
    if is_admin(getattr(user, "email", None)):
        return
    today = date.today().isoformat()
    rec = _assess_fit_counts.get(user.id)
    if not rec or rec.get("day") != today:
        rec = {"day": today, "n": 0}
        _assess_fit_counts[user.id] = rec
    rec["n"] += 1
    if rec["n"] > _ASSESS_FIT_DAILY_CAP:
        raise HTTPException(status_code=429, detail="Daily fit-assessment limit reached.")


_BROAD_DAILY_CAP = int(os.getenv("BROAD_DAILY_CAP", "40"))


@router.post("/tools/assess-fit")
def assess_fit_endpoint(req: AssessFitRequest, user=Depends(get_current_user)):
    """Decide whether the candidate should apply to a job (Fit Engine M1).

    Called by the extension BEFORE clicking Apply so it can skip clearly-wrong-fit jobs
    and record why. Capped per user/day (its own budget, not the letters quota) so it
    can't be looped to burn Anthropic spend, but generous enough for real campaigns.

    Broad mode is additionally capped at BROAD_DAILY_CAP applications/day to prevent
    spam patterns that trigger Indeed's anti-bot detection at the platform level.
    """
    _assess_fit_gate(user)
    profile = get_profile(user.id)

    if (profile.get("apply_mode") or "standard") == "broad":
        today_count = apps_db.count_today(user.id)
        if today_count >= _BROAD_DAILY_CAP:
            return {
                "fit_score": 0,
                "decision": "skip",
                "reason": f"Broad mode daily limit reached ({_BROAD_DAILY_CAP} applications). Resumes tomorrow.",
                "concerns": ["Daily Broad cap hit — account health protection"],
                "judged": True,
                "apply_mode": "broad",
            }

    result = assess_fit(
        job={"title": req.job_title, "company": req.company, "description": req.description},
        profile=profile,
        screener_questions=req.screener_questions,
    )
    return result


@router.post("/tools/answer-question")
def answer_question(req: AnswerQuestionRequest, user=Depends(get_current_user)):
    """Generate an answer for one employer screener question (Loop 4 filler).

    Open-text and ambiguous multiple-choice questions that the extension's rule-based
    filler can't handle. Deterministic cases (demographic decline, salary, Yes/No) are
    solved client-side and never reach here. Counts against the same daily AI quota as
    cover letters so it can't be abused to burn Anthropic spend.
    """
    _claim_ai_slot(user)
    profile = get_profile(user.id)
    try:
        answer = answer_screener_question(
            req.question,
            job={"title": req.job_title, "company": req.company},
            profile=profile,
            options=req.options,
        )
    except Exception:
        usage_db.release_today(user.id)
        raise
    if not answer:
        usage_db.release_today(user.id)
    return {"answer": answer}


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
    return {"configured": True, "count": len(responses), "emails": responses}


@router.get("/tools/stall-scan")
def stall_scan(user=Depends(get_current_user)):
    """Admin-only, read-only: what the stall watch currently sees for every running campaign.

    The background sweep (app/stall_watch.py) only speaks when something is wrong, which
    makes "is it actually working?" unanswerable in prod. This runs the same judgement and
    returns it — no activity lines written, no email sent.
    """
    if not is_admin(getattr(user, "email", None)):
        raise HTTPException(status_code=403, detail="admin_only")
    from app.stall_watch import STALL_FIRST_SECS, STALL_GAP_SECS, report

    verdicts = report()
    return {
        "running_campaigns": len(verdicts),
        "stalled": [v for v in verdicts if v["stalled"]],
        "verdicts": verdicts,
        "thresholds_secs": {"first_application": STALL_FIRST_SECS, "between": STALL_GAP_SECS},
    }


@router.get("/platform/inbox-urls")
def platform_inbox_urls(user=Depends(get_current_user)):
    profile = get_profile(user.id)
    enabled = profile.get("platforms", [])
    return {p: PLATFORM_INBOX_URLS[p] for p in enabled if p in PLATFORM_INBOX_URLS}


@router.get("/extension/download")
def download_extension(user=Depends(get_current_user)):
    import io
    import zipfile

    ext_dir = os.path.join(os.path.dirname(__file__), "..", "..", "chrome-extension")
    if not os.path.isdir(ext_dir):
        return JSONResponse(status_code=404, content={"error": "Extension folder not found"})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(ext_dir):
            for fname in files:
                full = os.path.join(root, fname)
                arcname = os.path.join("hiredrop-extension", os.path.relpath(full, ext_dir))
                zf.write(full, arcname)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=hiredrop-extension.zip"},
    )


class NormalizeKeywordsRequest(BaseModel):
    keywords: list[str] = []


@router.post("/tools/normalize-keywords")
def normalize_keywords_endpoint(body: NormalizeKeywordsRequest, user=Depends(get_current_user)):
    """Suggest typo fixes for job-search keywords (the one place a typo costs results).

    Returns only the terms with an obvious misspelling as {original, suggestion} — the UI
    shows them as an accept-with-one-click "did you mean", never a silent rewrite. Fail-open:
    [] on any error so a hiccup never blocks a search. Rate-limited like other AI endpoints
    so it can't be looped to burn Anthropic spend.
    """
    _ai_quota_gate(user)
    return {"corrections": normalize_keywords(body.keywords or [])}
