import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.routers import (
    activity,
    applications,
    auth,
    billing,
    campaign,
    email_processor,
    extension,
    jobs,
    profile,
    promo,
    review,
    tools,
)
from config import STALL_WATCH_ENABLED

_EMAIL_POLL_INTERVAL = int(os.getenv("EMAIL_POLL_INTERVAL_SECONDS", "1800"))  # 30 min default


async def _email_poll_loop() -> None:
    from app.db import applications as apps_db
    from modules.email_parser import check_email_responses

    status_map = {"interview_invite": "interview", "rejected": "rejected", "received": "received"}

    while True:
        await asyncio.sleep(_EMAIL_POLL_INTERVAL)
        try:
            for item in check_email_responses():
                company = item.get("company", "")
                new_status = status_map.get(item["email_status"])
                if not company or not new_status:
                    continue
                for app in apps_db.find_by_company_all_users(company):
                    if app["status"] != new_status:
                        apps_db.update_status(app["id"], new_status, app["user_id"])
        except Exception as exc:  # noqa: BLE001
            print(f"[email_poll] error: {exc}")


@asynccontextmanager
async def lifespan(app_: FastAPI):
    # Email response-tracking is OFF by default. It was crude (keyword subject matching) and
    # cross-tenant (find_by_company_all_users mutates every user's apps by company name from
    # ONE shared inbox), and check_email_responses() did BLOCKING imaplib IO right inside the
    # event loop — a hung IMAP call could stall the whole API. Set EMAIL_POLL_ENABLED=1 to
    # bring it back (and move it off the loop first).
    task = None
    if os.getenv("EMAIL_POLL_ENABLED", "").lower() in ("1", "true", "yes"):
        task = asyncio.create_task(_email_poll_loop())

    # Stall watch: "running, but applications aren't growing" — the failure the heartbeat
    # cannot see (app/stall_watch.py). ON by default; it only reports (activity line +
    # optional ops email), never flips a campaign flag.
    stall_task = None
    if STALL_WATCH_ENABLED:
        from app.stall_watch import watch_loop

        stall_task = asyncio.create_task(watch_loop())

    yield
    for t in (task, stall_task):
        if t:
            t.cancel()


app = FastAPI(title="HireDrop API", version="1.0.0", lifespan=lifespan)

# Job lists ship full descriptions and were going over the wire uncompressed —
# Railway doesn't compress for us. Small responses stay plain (gzip overhead).
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://hiredrop.io",
        "https://www.hiredrop.io",
        "http://localhost:3000",
    ],
    # Vercel preview deploys + the published chrome extension origin. The
    # extension regex is pinned to the Web Store extension ID (stable across
    # updates) instead of `chrome-extension://.*`, which would expose API
    # responses to ANY installed extension. NOTE: a locally-loaded *unpacked*
    # extension gets a random dev ID — add it here temporarily for local dev.
    # The `jobflow-website` pattern is a transition allowance until the Vercel
    # project is renamed; remove after rename + custom domain cutover.
    allow_origin_regex=r"^(chrome-extension://bjideoimenmpcpnhppneehmjplkgkede|https://(hiredrop-website|jobflow-website)[a-z0-9-]*\.vercel\.app)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request, call_next):
    """Baseline security headers on every response. This is a JSON API (no HTML
    rendered here), so a CSP isn't meaningful — that belongs on the Next.js
    frontend. These guard against MIME-sniffing, clickjacking, and downgrade.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.middleware("http")
async def request_timing(request, call_next):
    """Minimal latency audit while there's no real observability: every response
    carries its server-side time, and anything slower than a second lands in the
    Railway log — so "which call is the bottleneck" is answerable from the log
    instead of a guess. Also feeds the 5xx burst watch (app/ops_watch.py): both
    explicit 5xx responses and unhandled exceptions (which FastAPI turns into a
    500 above this middleware) are recorded."""
    from app.ops_watch import record_5xx

    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        record_5xx(request.url.path, 500)
        raise
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    response.headers["X-Server-Time-Ms"] = str(elapsed_ms)
    if elapsed_ms > 1000:
        print(f"[slow-request] {request.method} {request.url.path} took {elapsed_ms}ms")
    if response.status_code >= 500:
        record_5xx(request.url.path, response.status_code)
    return response


app.include_router(jobs.router, prefix="/api/v1")
app.include_router(applications.router, prefix="/api/v1")
app.include_router(campaign.router, prefix="/api/v1")
app.include_router(profile.router, prefix="/api/v1")
app.include_router(tools.router, prefix="/api/v1")
app.include_router(activity.router, prefix="/api/v1")
app.include_router(extension.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(email_processor.router, prefix="/api/v1")
app.include_router(promo.router, prefix="/api/v1")
app.include_router(billing.router, prefix="/api/v1")
app.include_router(review.router, prefix="/api/v1")


@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}
