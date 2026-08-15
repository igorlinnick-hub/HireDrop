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
    FREE_APP_LIMIT,
    MAX_PER_PLATFORM,
    daily_limit,
    get_free_apps_used,
    get_submit_mode,
    get_tier,
)
from app.deps import get_current_user
from app.disposable_email import is_disposable_email
from app.schemas import CampaignStartRequest

router = APIRouter(tags=["campaign"])

# Single source of truth for the caps lives in app/db/subscriptions.py — the
# extension used to hardcode its own 50 here and in content.js/background.js,
# which diverged from the ban-safety gate (MAX_PER_PLATFORM). Status now serves
# the authoritative numbers so the extension can enforce them PRE-submit (before
# the irreversible application) instead of discovering the 429 after the fact.


@router.get("/campaign/status")
def campaign_status(since: str | None = None, user=Depends(get_current_user)):
    # `since` = the client's LOCAL midnight as a UTC ISO instant, so "today" counts
    # roll over at the USER's midnight, not the server's UTC (2026-08-12 fix).
    # Effective state = flag AND fresh extension heartbeat; a zombie (laptop closed,
    # crash, offline — flag stuck true, no pings) self-heals here: reported not-running
    # and the row is lazily flipped. See ZOMBIE_FIX_PLAN.md.
    state = campaign_db.get_effective_state(user.id)
    profile = get_profile(user.id)
    enabled_platforms = profile.get("platforms", [])

    today_count = apps_db.count_today(user.id, since)
    platform_counts = apps_db.count_today_by_platform(user.id, since)
    jobs_ready = jobs_db.count_new_jobs(user.id, enabled_platforms)

    tier = get_tier(user.id, getattr(user, "email", None))
    submit_mode = get_submit_mode(user.id)
    free = tier == "free"

    return {
        "running": state["running"],
        "filters": state["filters"],
        "started_at": state["started_at"],
        "today_applications": today_count,
        "platform_counts": platform_counts,
        # Two-cap model: per-platform ceiling (ban-safety rail, counted per platform)
        # + total daily budget (tier value/cost, raised in tap mode). Extension enforces BOTH pre-submit.
        "limit_per_platform": MAX_PER_PLATFORM,
        "daily_limit": daily_limit(tier, submit_mode),
        "tier": tier,
        "submit_mode": submit_mode,
        "jobs_ready": jobs_ready,
        # Third cap, free tier only: the lifetime 40-app taste (FREE_TASTE_PLAN.md).
        # The extension stops the campaign when free_used hits free_limit; the
        # dashboard shows the paywall. None for paid/admin.
        "free_used": get_free_apps_used(user.id) if free else None,
        "free_limit": FREE_APP_LIMIT if free else None,
    }


@router.post("/campaign/start")
def campaign_start(req: CampaignStartRequest, user=Depends(get_current_user)):
    # Free-taste abuse guard: throwaway-email accounts never get to spend AI
    # budget. Signup is Supabase-hosted, so the first backend chokepoint is here.
    if is_disposable_email(getattr(user, "email", None)):
        raise HTTPException(
            status_code=403,
            detail="disposable_email",
        )
    # Fail-closed onboarding gate (defense-in-depth: the dashboard layout and
    # the extension's START_CAMPAIGN both check too). An un-onboarded profile
    # has no name/keywords/resume — a campaign would file applications with
    # blanks under the user's identity.
    profile = get_profile(user.id)
    if not profile.get("onboarding_completed"):
        raise HTTPException(status_code=403, detail="onboarding_incomplete")
    filters = {
        "keywords": req.keywords,
        "platforms": req.platforms,
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
    return {"started": True, "state": state}


@router.get("/campaign/readiness")
def campaign_readiness(user=Depends(get_current_user)):
    """What's left before a campaign can start meaningfully — the dashboard renders the
    failed checks as a checklist with deep-links instead of a Start that silently no-ops.
    (Extension installed/connected is checked client-side via the PING bridge.)"""
    from config import FREE_APP_LIMIT

    from app.db.subscriptions import get_free_apps_used

    profile = get_profile(user.id)
    state = campaign_db.get_effective_state(user.id)
    tier = get_tier(user.id, getattr(user, "email", None))
    submit_mode = get_submit_mode(user.id)
    free_used = get_free_apps_used(user.id) if tier == "free" else None
    return campaign_db.build_readiness(
        profile, state["running"], tier, submit_mode, free_used, FREE_APP_LIMIT
    )


@router.post("/campaign/stop")
def campaign_stop(user=Depends(get_current_user)):
    campaign_db.stop(user.id)
    try:
        get_supabase().table("campaign_screenshots").delete().eq("user_id", user.id).execute()
    except Exception:
        pass
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
        should_run = bool(campaign_db.get_state(user.id)["running"])
    except Exception:
        should_run = True  # fail-open: never stop a campaign on a state-read hiccup
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
    try:
        get_supabase().table("campaign_screenshots").upsert(
            {"user_id": user.id, "data": body.screenshot, "ts": time.time()},
            on_conflict="user_id",
        ).execute()
    except Exception:
        pass  # non-blocking — missed frame is fine
    return {"ok": True}


@router.get("/campaign/screenshot")
def get_screenshot(user=Depends(get_current_user)):
    try:
        res = get_supabase().table("campaign_screenshots").select("data,ts").eq("user_id", user.id).execute()
    except Exception:
        return {"data": None}
    if not res.data:
        return {"data": None}
    row = res.data[0]
    if time.time() - row["ts"] > 10:
        return {"data": None}
    return {"data": row["data"]}
