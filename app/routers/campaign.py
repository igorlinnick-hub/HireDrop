import contextlib
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db import activity as activity_db
from app.db import applications as apps_db
from app.db import campaign as campaign_db
from app.db import jobs as jobs_db
from app.db.client import get_supabase
from app.db.profile import get_profile
from app.db.subscriptions import (
    DEFAULT_MAX_PER_PLATFORM,
    FREE_APP_LIMIT,
    MAX_PER_PLATFORM,
    daily_limit,
    get_free_apps_used,
    get_submit_mode,
    get_tier,
    max_per_platform,
    read_submit_mode,
)
from app.db.user_day import day_start, remember_zone, stored_zone
from app.deps import get_current_user
from app.disposable_email import is_disposable_email
from app.schemas import CampaignStartRequest
from modules.ai_cover_letter import resume_text_for
from modules.ai_role_suggest import role_limit, suggest_roles
from modules.employer_answers import ANSWERS_UI, outside_us
from modules.employer_answers import missing as missing_answers
from modules.keyword_rotation import clean_keywords, complete_keywords
from modules.keyword_rotation import rotate as rotate_keywords
from modules.review_sheet import REVIEW_SINCE

router = APIRouter(tags=["campaign"])

# Single source of truth for the caps lives in app/db/subscriptions.py — the
# extension used to hardcode its own 50 here and in content.js/background.js,
# which diverged from the ban-safety gate (MAX_PER_PLATFORM). Status now serves
# the authoritative numbers so the extension can enforce them PRE-submit (before
# the irreversible application) instead of discovering the 429 after the fact.


# An AUTO run cannot submit a Lever approval — its form needs a human at the captcha, and
# nobody is watching. The extension has always dropped those locally, but the SERVER did not
# know, so it spent the day's budget slice on rows the run then discarded: a user whose
# approvals are Lever-heavy got a short queue (or "no approved jobs") while the dashboard
# showed the swipes waiting. Same rule, told to the server, so the slice goes to work the
# run can actually do. Kept as a list rather than a bare `if` because the next
# needs-a-human platform will land here too.
_AUTO_CANNOT_SUBMIT = ("lever",)


@router.get("/campaign/queue")
def campaign_queue(
    since: str | None = None, mode: str | None = None, user=Depends(get_current_user)
):
    """The work list for a Tap run — owned by the server, not by chrome.storage.

    Step 1 of moving campaign state server-side (decision 2026-09-08). Until now the
    extension built this itself from GET /jobs plus browser-local dedup sets, so nothing
    outside that one Chrome profile could answer "what is left" — no progress on the
    dashboard, no way for the phone to know, and a wiped profile re-applied to jobs
    already sent (the appliedUrls set was the only guard).

    Same rules the extension applies, decided here instead:
      * only rows the user actually approved by swiping,
      * only platforms an approved swipe can really be submitted to,
      * nothing already applied — matched by POSTING IDENTITY (#161), not URL text,
      * the per-platform ban-safety cap, then today's remaining tier budget.

    Read-only. It reports what is left and what was already spent; it never writes state.
    """
    from app.routers.jobs import TAP_APPLY_PLATFORMS
    from modules.job_identity import job_identity

    # The cap's own boundary when the user's zone is on file; the client's midnight is
    # only a display fallback for a user whose browser never reported one.
    zone = stored_zone(user.id)
    done_today = apps_db.count_today(user.id, day_start(zone) if zone else since)
    tier = get_tier(user.id, getattr(user, "email", None))
    budget = daily_limit(tier, get_submit_mode(user.id))
    budget_left = max(0, budget - done_today) if budget > 0 else 0

    applied_ids = {job_identity(u) for u in apps_db.applied_job_urls(user.id)}
    applied_ids.discard(None)

    # `mode` is the RUN's mode, not a preference: "auto" means unattended, so the platforms
    # that need a human are not candidates at all (see _AUTO_CANNOT_SUBMIT). Absent/unknown
    # mode keeps the old behaviour byte-for-byte — an older extension build must not lose
    # rows because it doesn't send the parameter yet.
    unattended = (mode or "").lower() == "auto"
    approved = [
        j
        for j in jobs_db.get_jobs(user.id)
        if (j.get("status") or "") == "approved"
        and (j.get("link") or j.get("apply_url"))
        and j.get("platform") in TAP_APPLY_PLATFORMS
        and not (unattended and j.get("platform") in _AUTO_CANNOT_SUBMIT)
    ]
    waiting = [
        j for j in approved if job_identity(j.get("link") or j.get("apply_url")) not in applied_ids
    ]

    # Per-platform ceiling first (ban safety is not negotiable), then the daily budget.
    per: dict[str, int] = {}
    capped: list[dict] = []
    for job in waiting:
        platform = job.get("platform")
        if per.get(platform, 0) >= max_per_platform(platform):
            continue
        per[platform] = per.get(platform, 0) + 1
        capped.append(job)

    queue = [
        {
            "id": j.get("id"),
            "title": j.get("title") or "",
            "company": j.get("company") or "",
            "platform": j.get("platform"),
            "apply_url": j.get("link") or j.get("apply_url"),
            "score": j.get("score"),
        }
        for j in capped[:budget_left]
    ]
    return {
        "queue": queue,
        "ready": len(queue),
        # Everything the user swiped that is still undone — before caps trim it. The
        # difference between this and `ready` is exactly what the caps are holding back,
        # and a queue that shrank silently is indistinguishable from a broken one.
        "waiting": len(waiting),
        "held_by_caps": len(waiting) - len(queue),
        "done_today": done_today,
        "daily_limit": budget,
        "cap_per_platform": DEFAULT_MAX_PER_PLATFORM,
        # Platforms under their own ceiling (LinkedIn 5) — absent from here means the default.
        "cap_by_platform": dict(MAX_PER_PLATFORM),
        # Rows the user approved that were already applied under another URL spelling.
        "already_applied": len(approved) - len(waiting),
    }


def start_refusal(
    user, profile: dict, answers_ui: int = ANSWERS_UI, state: dict | None = None
) -> str | None:
    """Why /campaign/start refuses this profile (its 403 detail), or None = it may start.

    One function so /campaign/status can tell the extension the same thing (see there).
    `state` is the campaign row when the caller already read it.
    """
    # Free-taste abuse guard: throwaway-email accounts never get to spend AI
    # budget. Signup is Supabase-hosted, so the first backend chokepoint is here.
    if is_disposable_email(getattr(user, "email", None)):
        return "disposable_email"
    # Fail-closed onboarding gate (defense-in-depth: the dashboard layout and
    # the extension's START_CAMPAIGN both check too). An un-onboarded profile
    # has no name/keywords/resume — a campaign would file applications with
    # blanks under the user's identity.
    if not profile.get("onboarding_completed"):
        return "onboarding_incomplete"
    # Same list /campaign/readiness shows. Enforced here too: a Start that skips the
    # dashboard's form would file 99%-complete applications that all come back.
    if outside_us(profile):
        return "us_only"
    # No Start without a resume, for every platform (Igor, 10-06) — readiness says the same.
    if not profile.get("resume_url"):
        return "resume_missing"
    # `answers_ui`: which questions the caller can ask (see employer_answers.SINCE). Saying
    # nothing means the current list — the extension sends nothing and never draws the form.
    if missing_answers(profile, answers_ui):
        return "employer_answers_missing"
    # Once, before the first run: the person checks everything we tell employers
    # (modules/review_sheet.py). Last, so it is asked of a profile that is otherwise ready.
    if answers_ui >= REVIEW_SINCE and campaign_db.review_due(user.id, profile, state):
        return "review_missing"
    return None


@router.get("/campaign/status")
def campaign_status(
    since: str | None = None, tz: str | None = None, user=Depends(get_current_user)
):
    # `tz` = the browser's IANA zone. Stored (app/db/user_day.py) so the CAP rolls over at
    # the user's midnight too, and then "today" here is counted from that same stored
    # midnight — one boundary for the number shown and the number enforced. `since` (the
    # client's local midnight) is only the fallback while no zone is on file.
    # Effective state = flag AND fresh extension heartbeat; a zombie (laptop closed,
    # crash, offline — flag stuck true, no pings) self-heals here: reported not-running
    # and the row is lazily flipped. See ZOMBIE_FIX_PLAN.md.
    state = campaign_db.get_effective_state(user.id)
    profile = get_profile(user.id)
    enabled_platforms = profile.get("platforms", [])

    zone = remember_zone(user.id, tz)
    day = day_start(zone) if zone else since
    today_count = apps_db.count_today(user.id, day)
    platform_counts = apps_db.count_today_by_platform(user.id, day)
    jobs_ready = jobs_db.count_new_jobs(user.id, enabled_platforms)
    # Swipes the user approved that are still undone. Reported in EVERY mode on purpose:
    # only a tap run consumes them, so an auto user who swiped (or swiped and then switched
    # back to Auto in Settings) has a stack nothing will ever pick up, and today nothing says
    # so. Found live 09-11: a real user has had 4 approved swipes waiting since 09-02.
    approved_waiting = jobs_db.count_approved_jobs(user.id)

    tier = get_tier(user.id, getattr(user, "email", None))
    # Say whether the mode is KNOWN, don't just hand over a fallback. The extension reads
    # this to decide auto-vs-tap, and a guessed "auto" there means applications a human
    # never approved (third layer of the #98 class: a value producible without evidence
    # is not evidence). Cap math keeps the conservative "auto" fallback either way.
    known_mode = read_submit_mode(user.id)
    submit_mode = known_mode or "auto"
    free = tier == "free"
    # The store extension swallows a 403 from /campaign/start and walks anyway, and the
    # server then stops it through /extension/ping within a minute — mid-form, with no
    # reason given. The one refusal every build since 1.7.9 honours BEFORE it opens
    # anything is an unknown mode, so a profile /campaign/start would refuse reads as
    # unknown here. Only while nothing is running: a run the dashboard started already
    # passed this gate, and nothing reads the flag mid-run. `start_refusal` says why.
    refusal = None if state["running"] else start_refusal(user, profile, state=state)

    return {
        "running": state["running"],
        "filters": state["filters"],
        "started_at": state["started_at"],
        "today_applications": today_count,
        "platform_counts": platform_counts,
        # Two-cap model: per-platform ceiling (ban-safety rail, counted per platform)
        # + total daily budget (tier value/cost, raised in tap mode). Extension enforces BOTH pre-submit.
        # `limit_per_platform` stays ONE number (the default) for every extension build in
        # the wild; `limit_by_platform` carries the platforms that run tighter (LinkedIn 5).
        "limit_per_platform": DEFAULT_MAX_PER_PLATFORM,
        "limit_by_platform": dict(MAX_PER_PLATFORM),
        "daily_limit": daily_limit(tier, submit_mode),
        "tier": tier,
        "submit_mode": submit_mode,
        "submit_mode_known": known_mode is not None and refusal is None,
        "start_refusal": refusal,
        "jobs_ready": jobs_ready,
        # How many roles this apply mode may carry (broad 7 / standard 5 / precise 3).
        # Served from the backend so the count lives in ONE place instead of being retyped
        # in every surface that shows the role picker.
        "role_limit": role_limit(profile.get("apply_mode")),
        # > 0 while submit_mode is "auto" means a stranded stack: the auto walk searches
        # platforms, it never reads approved rows. The surface that shows it must say that.
        "approved_waiting": approved_waiting,
        # Third cap, free tier only: the lifetime 40-app taste (FREE_TASTE_PLAN.md).
        # The extension stops the campaign when free_used hits free_limit; the
        # dashboard shows the paywall. None for paid/admin.
        "free_used": get_free_apps_used(user.id) if free else None,
        "free_limit": FREE_APP_LIMIT if free else None,
    }


@router.post("/campaign/start")
def campaign_start(
    req: CampaignStartRequest, answers_ui: int = ANSWERS_UI, user=Depends(get_current_user)
):
    profile = get_profile(user.id)
    refusal = start_refusal(user, profile, answers_ui)
    if refusal:
        raise HTTPException(status_code=403, detail=refusal)
    # Round-robin the roles: the walk always starts at index 0 and every cap counts
    # applications, so with six or seven roles the tail of the list never gets searched.
    # The server decides who leads this run and remembers it in the row it is about to
    # overwrite. See modules/keyword_rotation.
    prev_cursor = (campaign_db.get_state(user.id).get("filters") or {}).get("kw_cursor", 0)
    # Thin-input rescue (measured 2026-09-22): a run is only as good as the roles fed in,
    # and a handful of hand-typed words — one of them off-target — floored a whole run at
    # zero applies while every gate worked. If the user is below their apply mode's role
    # budget, top the set up from the résumé (the one source that isn't a blind guess),
    # bounded by the mode limit so we widen the net without inventing divergent roles.
    # Defensive + fail-open: an AI hiccup here must never block Start (the whole point is
    # to help thin accounts, not to add a failure mode to every campaign).
    run_keywords = list(req.keywords or [])
    added_from_resume: list[str] = []
    try:
        limit = role_limit(profile.get("apply_mode"))
        if len(clean_keywords(run_keywords)) < limit:
            resume_text = resume_text_for(profile)
            if resume_text:
                suggestions = suggest_roles(resume_text, limit=limit)
                run_keywords, added_from_resume = complete_keywords(
                    run_keywords, suggestions, limit
                )
    except Exception:  # noqa: BLE001 — Start must survive any suggestion failure
        run_keywords = list(req.keywords or [])
        added_from_resume = []
    if added_from_resume:
        with contextlib.suppress(Exception):
            activity_db.write(
                user.id,
                "Added "
                + str(len(added_from_resume))
                + " role(s) from your résumé to fill the day: "
                + ", ".join(added_from_resume),
                level="info",
                phase="campaign",
            )
    ordered_keywords, next_cursor = rotate_keywords(run_keywords, prev_cursor)
    # Lever's apply form is captcha-gated, so an AUTO run cannot finish one. That used to
    # block Start with a modal whose only button was "switch to Tap" — trading the whole
    # auto campaign for one board out of six, a swap nobody asked for (Igor, 09-15).
    # The run leaves Lever out instead and says so in the feed. Not a silent substitution
    # (#180): nothing is put in its place, the other platforms are the ones the user picked,
    # and the line names both the reason and the way to include it.
    run_platforms = list(req.platforms or [])
    skipped_platforms: list[str] = []
    if get_submit_mode(user.id) != "tap" and "lever" in run_platforms:
        if len(run_platforms) > 1:
            run_platforms = [p for p in run_platforms if p != "lever"]
            skipped_platforms = ["lever"]
            with contextlib.suppress(Exception):
                activity_db.write(
                    user.id,
                    "Lever is sitting this run out — its apply form needs a human for the "
                    "captcha. Switch to Tap to include it.",
                    level="info",
                    phase="campaign",
                )
        else:
            # Lever alone in auto mode leaves the run with nothing it can submit to.
            # Say that outright instead of starting a campaign that can only stall.
            raise HTTPException(status_code=400, detail="lever_needs_tap")
    filters = {
        "keywords": ordered_keywords,
        "kw_cursor": next_cursor,
        "platforms": run_platforms,
        "location": req.location,
        "job_type": req.job_type,
        # Geo-radius (miles) for non-remote searches. Sourced from the saved profile
        # (the radius picker writes it via /profile/radius) so it always matches what
        # the user set. Threaded here so it reaches chrome.storage campaignFilters, where
        # the extension URL builders turn it into Indeed radius= / LinkedIn distance=.
        # None = off (remote / no radius chosen) → builders omit the param (current behavior).
        "search_radius_miles": profile.get("search_radius_miles"),
    }
    state = campaign_db.start(user.id, filters)
    # The dashboard arms the extension from THIS response, not from its own local list —
    # otherwise the rotation would live only in the server's record of the run while the
    # browser kept searching role #1 first (QuickActions.tsx).
    return {
        "started": True,
        "state": state,
        "filters": filters,
        "skipped_platforms": skipped_platforms,
    }


@router.get("/campaign/readiness")
def campaign_readiness(answers_ui: int = ANSWERS_UI, user=Depends(get_current_user)):
    """What's left before a campaign can start meaningfully — the dashboard renders the
    failed checks as a checklist with deep-links instead of a Start that silently no-ops.
    (Extension installed/connected is checked client-side via the PING bridge.)"""
    from app.db.subscriptions import get_free_apps_used
    from config import FREE_APP_LIMIT

    profile = get_profile(user.id)
    state = campaign_db.get_effective_state(user.id)
    tier = get_tier(user.id, getattr(user, "email", None))
    submit_mode = get_submit_mode(user.id)
    free_used = get_free_apps_used(user.id) if tier == "free" else None
    review = answers_ui >= REVIEW_SINCE and campaign_db.review_due(user.id, profile, state)
    return campaign_db.build_readiness(
        profile,
        state["running"],
        tier,
        submit_mode,
        free_used,
        FREE_APP_LIMIT,
        answers_ui,
        review_due=review,
    )


@router.post("/campaign/stop")
def campaign_stop(user=Depends(get_current_user)):
    campaign_db.stop(user.id)
    with contextlib.suppress(Exception):
        get_supabase().table("campaign_screenshots").delete().eq("user_id", user.id).execute()
    return {"stopped": True}


class ExtensionPingBody(BaseModel):
    campaign_running: bool = False
    today_count: int = 0
    window_visible: bool = False
    last_screenshot_age: float | None = None
    error: str | None = None
    version: str | None = None
    # Which extension install is talking (chrome.runtime.id). Chrome runs the same
    # unpacked folder twice happily, and each copy has its own storage — so an idle twin
    # reports "campaign_running: false" under the same account while the real one is
    # mid-application. Logged on the reap path so the twin is visible, not deduced.
    instance_id: str | None = None


_ext_status: dict = {}  # user_id -> {ts, campaign_running, ...}


@router.post("/extension/ping")
def extension_ping(body: ExtensionPingBody, user=Depends(get_current_user)):
    _ext_status[user.id] = {
        "ts": time.time(),
        "campaign_running": body.campaign_running,
        "today_count": body.today_count,
        "window_visible": body.window_visible,
        "last_screenshot_age": body.last_screenshot_age,
        "error": body.error,
        "version": body.version,
        "instance_id": body.instance_id,
    }
    # Heartbeat stamp (ZOMBIE_FIX_PLAN): the TTL must measure whether the CAMPAIGN is
    # alive, not merely whether the extension is loaded. Stamping every ping conflated the
    # two and made zombies immortal — an idle extension pinging once a minute kept
    # last_ping_at fresh forever, so the TTL never expired and a `running` flag from days
    # ago still read as live (Igor hit exactly this: "Watch Live" 2 days after the run
    # died). So: only a ping that says the campaign is running counts as its heartbeat.
    # Stamped BEFORE should_run so a live extension whose SW just woke from a long sleep
    # refreshes first and is never told to stop by its own staleness. DB-persisted
    # (unlike _ext_status) → survives backend restarts.
    if body.campaign_running:
        campaign_db.touch_ping(user.id)
    else:
        # The extension is the only thing that can run a campaign; if it says it isn't,
        # a raised flag is a zombie. Clear it now instead of waiting out the TTL.
        # Reaping is a real event with real consequences (a half-filled application gets
        # abandoned), so it goes in the activity log. On 08-15 a run died mid-form and
        # this path was one of the suspects we could not confirm or clear, because it
        # left no trace at all.
        if campaign_db.reconcile_not_running(user.id):
            with contextlib.suppress(Exception):
                activity_db.write(
                    user.id,
                    "⏹ Campaign flag cleared — extension "
                    f"{body.instance_id or 'unknown'} (v{body.version or '?'}) reported it is not "
                    "running, and the campaign had gone silent too.",
                    level="warn",
                    phase="campaign",
                )
    # Return the backend's authoritative campaign flag so the extension can honor a Stop
    # even if the dashboard's postMessage stop was dropped (e.g. orphaned content script).
    try:
        state = campaign_db.get_state(user.id)
        should_run = bool(state["running"])
    except Exception:
        state = None
        should_run = True  # fail-open: never stop a campaign on a state-read hiccup
    # Persist which build is talking, only when it changed (steady state: zero extra
    # writes). The in-memory _ext_status copy dies with the worker; this one is the
    # durable source for /tools/ext-versions — store rollouts lag the repo silently,
    # and the lag must be visible without grepping activity logs (09-23, ext 1.8.3).
    if body.version and (state is None or state.get("ext_version") != body.version):
        campaign_db.record_ext_version(user.id, body.version)
    return {"ok": True, "should_run": should_run}


@router.get("/extension/ping")
def extension_status(user=Depends(get_current_user)):
    # `_ext_status` is per-process memory: it holds whatever the extension last POSTed to
    # THIS worker. After a restart (or on another worker) it is empty or stale, so the
    # echoed `campaign_running` can say "false" about a campaign that is happily applying
    # — it cost an hour of chasing a phantom stop on 08-15. Liveness of the CAMPAIGN has
    # exactly one source of truth, the DB flag, so serve that instead of the echo.
    row = _ext_status.get(user.id)
    try:
        running = bool(campaign_db.get_state(user.id)["running"])
    except Exception:
        running = None
    if not row:
        return {"online": False, "campaign_running": running}
    age = time.time() - row["ts"]
    return {
        "online": age < 60,
        "last_seen_secs_ago": round(age),
        **{k: v for k, v in row.items() if k != "ts"},
        # Overrides the echoed value on purpose — see the comment above.
        **({"campaign_running": running} if running is not None else {}),
    }


class ScreenshotBody(BaseModel):
    screenshot: str


@router.post("/campaign/screenshot")
def upload_screenshot(body: ScreenshotBody, user=Depends(get_current_user)):
    # non-blocking — missed frame is fine
    with contextlib.suppress(Exception):
        get_supabase().table("campaign_screenshots").upsert(
            {"user_id": user.id, "data": body.screenshot, "ts": time.time()},
            on_conflict="user_id",
        ).execute()
    return {"ok": True}


@router.get("/campaign/screenshot")
def get_screenshot(user=Depends(get_current_user)):
    try:
        res = (
            get_supabase()
            .table("campaign_screenshots")
            .select("data,ts")
            .eq("user_id", user.id)
            .execute()
        )
    except Exception:
        return {"data": None}
    if not res.data:
        return {"data": None}
    row = res.data[0]
    if time.time() - row["ts"] > 10:
        return {"data": None}
    return {"data": row["data"]}
