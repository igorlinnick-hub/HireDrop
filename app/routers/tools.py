import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import contextlib

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.db import applications as apps_db
from app.db import handbacks as handbacks_db
from app.db import jobs as jobs_db
from app.db import screener_cache
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
from modules.ai_cover_letter import generate_cover_letter, resume_text_for
from modules.ai_fit_judge import assess_fit
from modules.ai_keyword_normalize import normalize_keywords
from modules.ai_question_answer import answer_screener_question
from modules.ai_role_suggest import ROLE_LIMITS, role_limit, suggest_roles

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
        # Still effectively unlimited for normal use, but not literally unbounded:
        # internal accounts were the only ones that could burn spend forever.
        if not usage_db.claim_today(user.id, usage_db.admin_ceiling()):
            raise HTTPException(
                status_code=429,
                detail="Internal daily AI ceiling reached. Raise ADMIN_AI_DAILY_MAX or wait for tomorrow.",
            )
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


_role_suggest_counts: dict = {}
_ROLE_SUGGEST_DAILY_CAP = int(os.getenv("ROLE_SUGGEST_DAILY_CAP", "20"))


@router.get("/tools/suggest-roles")
def suggest_roles_endpoint(mode: str | None = None, user=Depends(get_current_user)):
    """Roles to offer the user, read off the resume they already uploaded.

    Setup asks "which roles do you want?" and an empty field is answered badly or not at
    all — Igor's own account searched "ai engineer" against a marketing resume for weeks.
    The resume is the one source that cannot be a blind guess, and it is already stored.

    Also serves `limit`: how many roles this apply mode may carry (broad 7 / standard 5 /
    precise 3). The UI enforces the count, so the number has to come from here rather than
    being retyped in the dashboard — the same mistake the $12/$39 prices made in 8 files.

    Capped per user/day like the other AI helpers: a few calls cover real setup, a loop
    cannot burn Anthropic budget.
    """
    if not is_admin(getattr(user, "email", None)):
        today = date.today().isoformat()
        rec = _role_suggest_counts.get(user.id)
        if not rec or rec.get("day") != today:
            rec = {"day": today, "n": 0}
            _role_suggest_counts[user.id] = rec
        rec["n"] += 1
        if rec["n"] > _ROLE_SUGGEST_DAILY_CAP:
            raise HTTPException(status_code=429, detail="Daily role-suggestion limit reached.")

    profile = get_profile(user.id)
    limit = role_limit(mode or profile.get("apply_mode"))
    resume_text = resume_text_for(profile)
    roles = suggest_roles(resume_text, limit=max(ROLE_LIMITS.values()))
    return {
        "roles": roles,
        # The cap for the mode asked about; `roles` deliberately carries MORE than this so
        # the user picks from a real list instead of being handed a pre-trimmed one.
        "limit": limit,
        "limits": ROLE_LIMITS,
        "mode": (mode or profile.get("apply_mode") or "standard"),
        # "none" = nothing to read: the surface must say "add your resume first" instead of
        # showing an empty list that looks like a broken call.
        "source": "resume" if roles else "none",
    }


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


def _match_human_answer(question: str, human: dict[str, str], options: list[str]) -> str | None:
    """The human's answer for this question, or None.

    Labels drift between the hand-back and the retry — a "(required)" marker appears,
    whitespace collapses, an asterisk shows up. The same normalisation the answer cache
    uses handles that, and nothing looser: a near-match on a screener question can put
    the answer to "3 years of Python?" into "3 years of Java?".

    For a multiple-choice field the stored answer is snapped back to a real option, so
    an answer saved before the form reworded its choices can't be typed in as free text.
    """
    from app.db.screener_cache import _normalise

    want = _normalise(question)
    if not want:
        return None
    for stored_q, stored_a in human.items():
        if _normalise(stored_q) != want:
            continue
        answer = (stored_a or "").strip()
        if not answer:
            return None
        if not options:
            return answer
        low = answer.lower()
        for o in options:
            if o.strip().lower() == low:
                return o
        return None  # the options changed — let the normal path decide
    return None


@router.post("/tools/answer-question")
def answer_question(req: AnswerQuestionRequest, user=Depends(get_current_user)):
    """Generate an answer for one employer screener question (Loop 4 filler).

    Open-text and ambiguous multiple-choice questions that the extension's rule-based
    filler can't handle. Deterministic cases (demographic decline, salary, Yes/No) are
    solved client-side and never reach here. Counts against the same daily AI quota as
    cover letters so it can't be abused to burn Anthropic spend.

    Multiple-choice answers are cached per (user, question, options, profile) — the
    same screener recurs on form after form, and re-deriving the answer both cost a
    Sonnet call and let one candidate give two different answers to one question.
    The cache is consulted BEFORE the quota is claimed: a repeat must not spend a
    slot, or the budget still drains at the old rate and only the bill improves.
    """
    profile = get_profile(user.id)

    # A human's own answer outranks everything — cache and model both. This is the
    # return leg of the hand-back loop (Igor, 09-21): the filler left a question blank,
    # the dashboard asked the person, and the job went back into the queue. On this
    # retry the answer is already here, so the form fills straight through.
    # Checked before the quota claim for the same reason the cache is: a question we
    # already have the answer to must not spend a slot.
    if req.job_id:
        human = handbacks_db.answers_for_job(user.id, req.job_id)
        if human:
            answer = _match_human_answer(req.question, human, req.options)
            if answer:
                return {"answer": answer, "from_user": True}

    cache_key = screener_cache.build_key(req.question, req.options, profile)
    if cache_key:
        cached = screener_cache.get(user.id, cache_key)
        if cached:
            screener_cache.touch(user.id, cache_key)
            return {"answer": cached, "cached": True}

    _claim_ai_slot(user)
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
    elif cache_key:
        screener_cache.put(user.id, cache_key, req.question, answer)
    return {"answer": answer, "cached": False}


@router.post("/tools/cover-letter-template")
def save_letter_template(req: TemplateRequest, user=Depends(get_current_user)):
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    with open(os.path.join(TEMPLATES_DIR, "cover_letter.txt"), "w") as f:
        f.write(req.template)
    return {"saved": True}


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


@router.get("/tools/ext-versions")
def ext_versions(user=Depends(get_current_user)):
    """Admin-only: which extension builds the fleet actually runs, vs the repo's latest.

    Born 09-23: a user's "bug" (the scary 'Campaign stopped' card) was ext 1.8.3 from the
    store while the repo was at 1.8.15 — 12 releases of fixes never left the building, and
    the only way to see it was grepping 'resuming (ext …)' out of one user's activity log.
    This makes the store lag a number: versions → user counts, plus who is behind.

    `ext_version` lands on the first /extension/ping after this deploy; 'unknown' rows are
    users whose extension hasn't pinged since (or was never installed).
    """
    if not is_admin(getattr(user, "email", None)):
        raise HTTPException(status_code=403, detail="admin_only")
    import json

    from app.db.client import fetch_paged, get_supabase

    latest = None
    with contextlib.suppress(Exception):
        manifest = os.path.join(
            os.path.dirname(__file__), "..", "..", "chrome-extension", "manifest.json"
        )
        with open(manifest) as f:
            latest = json.load(f).get("version")

    rows = fetch_paged(
        lambda start, end: (
            get_supabase()
            .table("campaign_states")
            .select("user_id, ext_version, ext_version_at, last_ping_at, running")
            .order("user_id")
            .range(start, end)
        ),
        limit=10_000,
    )

    versions: dict[str, dict] = {}
    for r in rows:
        v = r.get("ext_version") or "unknown"
        d = versions.setdefault(v, {"users": 0, "running_now": 0, "last_seen": None})
        d["users"] += 1
        d["running_now"] += bool(r.get("running"))
        seen = r.get("ext_version_at")
        if seen and (d["last_seen"] is None or seen > d["last_seen"]):
            d["last_seen"] = seen

    def _key(v: str):  # numeric semver ordering, newest first; 'unknown' sinks to the end
        try:
            return (0, [-int(p) for p in v.split(".")])
        except ValueError:
            return (1, [])

    return {
        "latest_in_repo": latest,
        "versions": {v: versions[v] for v in sorted(versions, key=_key)},
        "users_behind": sum(
            d["users"] for v, d in versions.items() if v != "unknown" and v != latest
        ),
        "users_unknown": versions.get("unknown", {}).get("users", 0),
    }


@router.get("/tools/run-report")
def run_report(window_hours: int = 6, user=Depends(get_current_user)):
    """What YOUR last run produced, and where the time went — not whether it was alive.

    Liveness was the wrong metric: on 2026-09-08 a walk ran 20 minutes, opened dozens of
    postings and applied to zero (every one skipped on fit) while every health signal
    stayed green. This returns the funnel (opened → applied), the yield (minutes per
    application) and one sentence naming the dominant loss.

    Scoped to the CURRENT run when one is going (campaign started_at), otherwise the last
    `window_hours`. Own data only — no admin gate needed, and none of it is cross-user.
    """
    from app.db import activity as activity_db
    from app.db import campaign as campaign_db

    started_at = None
    with contextlib.suppress(Exception):
        started_at = campaign_db.get_effective_state(user.id).get("started_at")
    return activity_db.run_report(user.id, since=started_at, window_hours=window_hours)


@router.get("/tools/ops-scan")
def ops_scan(user=Depends(get_current_user)):
    """Admin-only, read-only: what the ops watch sees — recent 5xx and per-platform
    scrape zero-streaks (app/ops_watch.py). Per-worker view: Railway runs 2 uvicorn
    workers, each counts its own traffic, so hit it twice to see both."""
    if not is_admin(getattr(user, "email", None)):
        raise HTTPException(status_code=403, detail="admin_only")
    from app.ops_watch import report

    return report()


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
