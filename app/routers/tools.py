import html
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
from app.db.user_day import user_day_start
from app.deps import get_current_user
from app.schemas import (
    AnswerQuestionRequest,
    AssessFitBatchRequest,
    AssessFitRequest,
    CoverLetterRequest,
    LetterPreviewRequest,
)
from config import RATE_LIMIT_ENFORCE, RATE_LIMIT_LETTERS_PER_DAY
from modules.ai_cover_letter import generate_cover_letter, resume_text_for
from modules.ai_fit_judge import assess_fit, clears_bar
from modules.ai_keyword_normalize import normalize_keywords
from modules.ai_question_answer import answer_screener_question
from modules.ai_role_suggest import ROLE_LIMITS, role_limit, suggest_roles
from modules.fit_queue import (
    COMPANY_WINDOW_DAYS,
    companies_holding_slots,
    company_key,
    company_slot_taken,
)
from modules.text_style import no_long_dashes

router = APIRouter(tags=["tools"])


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
        # Rolling 24h, for the dashboard tile; applications_today stays the cap's
        # local-day count (the History "Today" card and the meter read that one).
        "applications_last_24h": apps_db.count_last_hours(user.id, 24),
        "new_today": jobs_db.count_jobs_found_today(user.id),
        "tier": usage["tier"],
        "daily_limit": usage["daily_limit"],
        "remaining_today": usage["remaining_today"],
        "platform_counts": usage["platform_counts"],
        "max_per_platform": usage["max_per_platform"],
        "max_by_platform": usage.get("max_by_platform", {}),
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


def _company_capped(
    user_id: str, company: str, taken: list[str] | None = None, retried: set[str] | None = None
) -> bool:
    """Is this company's one slot taken — unless the person retried a hand-back there?

    The judge gets a company name, no URL. A "Try again" posting already passed the cap in
    build_queue (#353); the ATS walk then asks this judge, and the company's other open
    hand-backs would cap it right back (skeptic on #359). So a company with a recent retried
    ATS hand-back is not capped here while that retry is open (handbacks.requeued_companies
    bounds it).
    """
    if not company_slot_taken(
        company, companies_holding_slots(user_id) if taken is None else taken
    ):
        return False
    if retried is None:
        retried = _retried_company_keys(user_id)
    return company_key(company) not in retried


def _retried_company_keys(user_id: str) -> set[str]:
    try:
        return {company_key(c) for c in handbacks_db.requeued_companies(user_id)}
    except Exception as e:  # noqa: BLE001 — unreadable retries: the cap stands
        print(f"[assess-fit] retried hand-backs unreadable: {e}", file=sys.stderr)
        return set()


@router.post("/tools/assess-fit")
def assess_fit_endpoint(req: AssessFitRequest, user=Depends(get_current_user)):
    """Decide whether the candidate should apply to a job (Fit Engine M1).

    Called by the extension BEFORE clicking Apply so it can skip clearly-wrong-fit jobs
    and record why. Capped per user/day (its own budget, not the letters quota) so it
    can't be looped to burn Anthropic spend, but generous enough for real campaigns.

    Broad mode is additionally capped at BROAD_DAILY_CAP applications/day to prevent
    spam patterns that trigger Indeed's anti-bot detection at the platform level.
    """
    profile = get_profile(user.id)

    broad_capped = _broad_cap_verdict(user.id, profile)
    if broad_capped:
        return broad_capped

    # One application per company per 60 days, on the live walks too (Igor, 10-06). The
    # Indeed and ZipRecruiter walks never pass through the server queue, so before this they
    # applied to a company the queue would have capped: 6 second postings at one employer in
    # 60 days, 5 of them Indeed. Same read as the queue (fit_queue.companies_holding_slots),
    # and before the judge, so a capped posting costs no AI call.
    if _company_capped(user.id, req.company):
        # fit_score None, not 0: this is not a bad fit, and the extension prints the score.
        # The reason opens with "Company cap" — activity._categorize counts it as its own
        # loss instead of "fit gate" (run-report).
        return {
            "fit_score": None,
            "decision": "skip",
            "reason": f"Company cap — already tried {req.company} in the last "
            f"{COMPANY_WINDOW_DAYS} days, one application per company.",
            "concerns": ["Company cap — one application per employer per 60 days"],
            "judged": True,
            "company_capped": True,
        }

    resume_text = None
    fresh_because = None
    if req.job_id:
        reused, resume_text, fresh_because = _stored_verdict(user.id, profile, req.job_id)
        if reused is not None:
            return reused

    # The daily budget guards Anthropic spend, so it is charged here — at the model call —
    # and not for a cap or a stored verdict, which cost none (a search-page-judged Indeed
    # card used to pay twice: once in the batch, once again on its job page).
    _assess_fit_gate(user)
    result = assess_fit(
        job={"title": req.job_title, "company": req.company, "description": req.description},
        profile=profile,
        screener_questions=req.screener_questions,
        resume_text=resume_text,
    )
    if isinstance(result, dict) and result.get("judged"):
        result["verdict_source"] = "live"
        if fresh_because:
            result["fresh_because"] = fresh_because
    return result


def _broad_cap_verdict(user_id: str, profile: dict) -> dict | None:
    """The skip every Broad-mode posting gets once today's Broad cap is spent, else None."""
    if (profile.get("apply_mode") or "standard") != "broad":
        return None
    if apps_db.count_today(user_id, user_day_start(user_id)) < _BROAD_DAILY_CAP:
        return None
    return {
        "fit_score": 0,
        "decision": "skip",
        "reason": f"Broad mode daily limit reached ({_BROAD_DAILY_CAP} applications). Resumes tomorrow.",
        "concerns": ["Daily Broad cap hit — account health protection"],
        "judged": True,
        "apply_mode": "broad",
    }


def _assess_fit_budget(user, want: int) -> int:
    """Claim up to `want` judge calls from today's per-user fit budget; returns how many."""
    if want <= 0 or is_admin(getattr(user, "email", None)):
        return max(0, want)
    today = date.today().isoformat()
    rec = _assess_fit_counts.get(user.id)
    if not rec or rec.get("day") != today:
        rec = {"day": today, "n": 0}
        _assess_fit_counts[user.id] = rec
    take = max(0, min(want, _ASSESS_FIT_DAILY_CAP - rec["n"]))
    rec["n"] += take
    return take


def _refund_assess_fit(user, n: int) -> None:
    """Hand back budget claimed by _assess_fit_budget for calls that never started."""
    rec = _assess_fit_counts.get(user.id)
    if n > 0 and rec and rec.get("day") == date.today().isoformat():
        rec["n"] = max(0, rec["n"] - n)


# The search-page judge runs while the extension waits on the results page, so it has a
# deadline: what is not judged by then goes back as "unjudged" and the job page judges it
# live, exactly as before this endpoint existed. Haiku answers in ~2-4 s and a near-bar
# score escalates to Sonnet (~5-10 s), so 10 workers clear a 15-card page in one or two
# rounds. The whole request must answer inside 30 s: Chrome terminates an extension service
# worker whose fetch() waits longer (background.js PREJUDGE_CARDS races 28 s), and the DB
# reads/writes around the judge take a few seconds of their own.
_BATCH_WORKERS = 10
_BATCH_DEADLINE_S = 16.0
_HARVEST_PLATFORMS = ("indeed", "ziprecruiter")
# Below this it is a card snippet, not a posting (/jobs/describe's MIN_STORABLE_DESC). A
# verdict on a snippet would be stored and reused for every later run (#192: thin text
# scores high), so such a card goes back unjudged and its job page judges it on the text.
_MIN_JUDGE_TEXT = 300
# Each batch holds a request thread up to the judge deadline plus its own 10 judge
# threads. Past this many at once per process, a page goes back unjudged — the old live
# path — instead of queueing behind the others.
_BATCH_SLOTS = threading.BoundedSemaphore(8)
# Pool statuses the person (or a dead-link report) already closed: never judged, never opened.
_CLOSED_STATUSES = {"skipped", "rejected", "dismissed", "dead"}
# The person said yes in Tap: no judge here; the job page decides as it always has.
_PICKED_STATUSES = {"approved", "queued"}
_TAG = re.compile(r"<[^>]+>")
# What jobs_db.update_job_description keeps; a longer text would read as "new" every run.
_MAX_TEXT = 5000


def _posting_text(raw: str) -> str:
    """Posting text from what the card sent — the extension strips Indeed's HTML, this only
    makes sure a tag that slipped through is not judged or stored as words. Line breaks
    stay: tailoring and the interview kit read this text too."""
    text = html.unescape(_TAG.sub(" ", raw or ""))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)[:_MAX_TEXT]


@router.post("/tools/assess-fit-batch")
def assess_fit_batch_endpoint(req: AssessFitBatchRequest, user=Depends(get_current_user)):
    """Judge a whole search-results page AHEAD of opening any posting on it.

    The Indeed walk used to open every card, read it, ask /tools/assess-fit, and usually
    skip: ~18 s per rejected posting, one at a time (10-07: 18 rejections in a row, 0
    applied, then the platform ran dry). It also threw every verdict away — 0 of 767
    Indeed pool rows held one — so a third of all judge calls since 09-22 re-judged a
    posting already rejected (INNIO six times).

    The server still cannot read Indeed (403). The person's browser can, and Indeed's own
    results page fetches every card's full posting in one call (/rpc/jobdescs, ~0.4 s for
    30, live 10-07). So the extension sends the page's cards WITH their text, and this:

      * saves the text on the pool row (inserting rows the harvest has not saved yet);
      * reuses a verdict the row already holds for the CURRENT profile version — the same
        fit_version rule as the ATS queue (fit_queue.has_current_verdict);
      * judges the rest in parallel (fit_queue.judge_pending, which stores each verdict);
      * applies the one-per-company cap and the Broad daily cap before any AI call.

    -> one result per card: decision apply / skip / unjudged, with the pool row id. The
    extension opens only "apply" (and "unjudged", which the job page judges live), and
    sends the id back to /tools/assess-fit, which then answers from the stored verdict.
    Anything that fails here degrades to "unjudged" — the old one-at-a-time path — never
    to a skip the judge did not make.
    """
    if not _BATCH_SLOTS.acquire(blocking=False):
        return {
            "results": [
                {"link": c.link, "job_id": None, "decision": "unjudged", "source": "busy"}
                for c in req.jobs
            ],
            "judged": 0,
            "reused": 0,
            "unjudged": len(req.jobs),
        }
    try:
        return _assess_fit_batch(req, user)
    finally:
        _BATCH_SLOTS.release()


def _assess_fit_batch(req: AssessFitBatchRequest, user) -> dict:
    from app.routers.jobs import _deck_resume_text
    from modules.ai_fit_judge import mode_threshold, verdict_version
    from modules.fit_queue import has_current_verdict, judge_pending

    started = time.monotonic()
    cards, seen = [], set()
    for c in req.jobs:
        if c.platform in _HARVEST_PLATFORMS and c.link and c.title and c.link not in seen:
            seen.add(c.link)
            cards.append(c)
    if not cards:
        return {"results": [], "judged": 0, "reused": 0, "unjudged": 0}

    profile = get_profile(user.id)

    def result(card, decision: str, source: str, row: dict | None = None, **extra) -> dict:
        return {
            "link": card.link,
            "job_id": (row or {}).get("id"),
            "decision": decision,
            "source": source,
            **extra,
        }

    broad_capped = _broad_cap_verdict(user.id, profile)
    if broad_capped:
        return {
            "results": [
                result(c, "skip", "broad_cap", reason=broad_capped["reason"]) for c in cards
            ],
            "judged": 0,
            "reused": 0,
            "unjudged": 0,
        }

    texts = {c.link: _posting_text(c.description) for c in cards}
    try:
        rows = jobs_db.rows_by_links(user.id, [c.link for c in cards])
        missing = [c for c in cards if c.link not in rows]
        inserted = {c.link for c in missing}
        if missing:
            jobs_db.save_jobs_bulk(
                user.id,
                [
                    {
                        "title": c.title,
                        "company": c.company,
                        "link": c.link,
                        "status": "new",
                        "platform": c.platform,
                        "description": texts[c.link],
                        "location": c.location,
                    }
                    for c in missing
                ],
                insert_only=True,
            )
            rows.update(jobs_db.rows_by_links(user.id, [c.link for c in missing]))
    except Exception as e:  # noqa: BLE001 — no pool rows: the walk judges live, as before
        print(f"[assess-fit-batch] pool unreadable: {e}", file=sys.stderr)
        return {
            "results": [result(c, "unjudged", "pool_unreadable") for c in cards],
            "judged": 0,
            "reused": 0,
            "unjudged": len(cards),
        }

    taken = companies_holding_slots(user.id)
    retried = None
    out: dict = {}
    to_judge: list[tuple] = []
    for c in cards:
        row = rows.get(c.link)
        if not row:
            out[c.link] = result(c, "unjudged", "not_saved")
            continue
        status = row.get("status") or "new"
        if status == "applied":
            out[c.link] = result(c, "skip", "applied", row, reason="Already applied")
            continue
        if status in _CLOSED_STATUSES:
            out[c.link] = result(c, "skip", "dismissed", row, reason="Passed on earlier")
            continue
        if status in _PICKED_STATUSES:
            out[c.link] = result(c, "unjudged", "picked", row)
            continue
        if company_slot_taken(c.company, taken):
            if retried is None:
                retried = _retried_company_keys(user.id)
            if _company_capped(user.id, c.company, taken, retried):
                out[c.link] = result(
                    c,
                    "skip",
                    "company_cap",
                    row,
                    reason=f"Company cap — already tried {c.company} in the last "
                    f"{COMPANY_WINDOW_DAYS} days, one application per company.",
                )
                continue
        # Judge on the longer of the two texts: the card's fresh posting, or what the row
        # already holds (an earlier detail-page read).
        if len(texts[c.link]) > len(row.get("description") or "") or c.link in inserted:
            row["_new_text"] = texts[c.link]
            row["description"] = texts[c.link]
        if len((row.get("description") or "").strip()) < _MIN_JUDGE_TEXT:
            out[c.link] = result(c, "unjudged", "thin_text", row)
            continue
        to_judge.append((c, row))

    resume_text = _deck_resume_text(user.id, profile)
    version = verdict_version(profile, resume_text)
    threshold = mode_threshold(profile)
    stored_before = {row["id"] for _, row in to_judge if has_current_verdict(row, version)}
    pending = [row for _, row in to_judge if row["id"] not in stored_before]
    judged = 0
    if pending:
        budget = _assess_fit_budget(user, len(pending))
        stats: dict = {}
        judged = judge_pending(
            user.id,
            profile,
            pending,
            max_calls=budget,
            deadline_s=_BATCH_DEADLINE_S,
            workers=_BATCH_WORKERS,
            resume_text=resume_text,
            version=version,
            stats=stats,
        )
        _refund_assess_fit(user, budget - stats.get("submitted", 0))

    for c, row in to_judge:
        if not has_current_verdict(row, version):
            out[c.link] = result(c, "unjudged", "judge_unavailable", row)
            continue
        score = int(row["fit_score"])
        out[c.link] = result(
            c,
            "apply" if clears_bar(score, threshold) else "skip",
            "stored" if row["id"] in stored_before else "judged",
            row,
            fit_score=score,
            reason=row.get("fit_reason") or "",
            threshold=threshold,
        )

    # The posting text, stored after the judge so the harvest's snippet insert (fired a
    # moment earlier for the same cards) cannot land on top of it. Text only, never status.
    texts_to_store = [(row["id"], row["_new_text"]) for _, row in to_judge if row.get("_new_text")]
    if texts_to_store:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(
                    lambda t: jobs_db.update_job_description(t[0], user.id, t[1]), texts_to_store
                )
            )

    results = [out[c.link] for c in cards]
    return {
        "results": results,
        "judged": judged,
        "reused": len(stored_before),
        "unjudged": sum(1 for r in results if r["decision"] == "unjudged"),
        "ms": int((time.monotonic() - started) * 1000),
    }


def _stored_verdict(
    user_id: str, profile: dict, job_id: str
) -> tuple[dict | None, str | None, str]:
    """The verdict the server queue already holds for this pool row, if it is still true.

    -> (verdict or None, resume text read on the way, why a fresh judge is needed).

    The ATS walk opens the postings the queue served — rows the server judged on the full
    description and kept because they cleared the user's bar. Judging them again on the page
    text was a second authority over the same decision (#184's class): 10-02 the queue held
    Wikimedia 38 / Grafana 42 / Hightouch 38 on a broad bar of 35 and the live judge said
    22-30, so three of five opened postings were skipped by a verdict the list contradicted.

    STALE = the row's fit_version is not the current ai_fit_judge.verdict_version() — the
    fingerprint of prompt version + apply mode + preferences/keywords + resume text. Edit the
    resume, switch the mode or change the keywords and every stored verdict is stale, here
    exactly as in the queue (fit_queue.has_current_verdict). No score, a stale score, or a
    row that is not this user's -> judge live, as before. The bar is the same clears_bar()
    the queue filtered with.
    """
    from modules.ai_fit_judge import clears_bar, mode_threshold, verdict_version
    from modules.fit_queue import has_current_verdict

    try:
        # Filtered by user_id: service_role bypasses RLS (IDOR rule) — another user's row
        # id reads as "not in your pool" and gets the live judge.
        row = jobs_db.get_job_by_id(user_id, job_id)
    except Exception as e:  # noqa: BLE001 — an unreadable row costs a judge call, nothing else
        print(f"[assess-fit] pool row unreadable: {e}", file=sys.stderr)
        return None, None, "stored score unreadable"
    if not row:
        return None, None, "not in your list"
    if row.get("fit_score") is None:
        return None, None, "no stored score"
    resume_text = resume_text_for(profile)
    version = verdict_version(profile, resume_text)
    if not has_current_verdict(row, version):
        return None, resume_text, "profile or resume changed since it was scored"

    threshold = mode_threshold(profile)
    score = int(row["fit_score"])
    mode = profile.get("apply_mode") or "standard"
    if mode not in ("broad", "standard", "precise"):
        mode = "standard"  # as assess_fit and mode_threshold read an unknown mode
    return (
        {
            "fit_score": score,
            "decision": "apply" if clears_bar(score, threshold) else "skip",
            "reason": row.get("fit_reason") or "",
            "concerns": [],
            "judged": True,
            "apply_mode": mode,
            "threshold": threshold,
            "judge_model": row.get("fit_model") or "",
            "escalated": False,
            "model_decision": None,
            # The log says which verdict decided: the extension prints "from your list"
            # for this and "judged now" for a live one.
            "verdict_source": "queue",
            "judged_at": row.get("fit_judged_at"),
        },
        resume_text,
        "",
    )


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
            # Cached before the no-long-dash rule (text_style) — clean on the way out,
            # and write the clean text back so the legacy row is migrated once instead
            # of being re-cleaned on every hit (touch() keeps it alive indefinitely).
            # An option is returned verbatim: it must still match the form's wording.
            if not req.options:
                cleaned = no_long_dashes(cached)
                if cleaned != cached:
                    screener_cache.put(user.id, cache_key, req.question, cleaned)
                cached = cleaned
            return {"answer": cached, "cached": True}

    _claim_ai_slot(user)
    try:
        # The posting text, when we hold the row: the extension sends only title and
        # company, and a model that cannot read the posting invents the employer
        # ("Why us?" answered about the wrong industry — dry-run 09-27).
        description = req.job_description or ""
        if req.job_id and not description:
            with contextlib.suppress(Exception):
                row = jobs_db.get_job_by_id(user.id, req.job_id)
                description = (row or {}).get("description") or ""
        answer = answer_screener_question(
            req.question,
            job={"title": req.job_title, "company": req.company, "description": description},
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
