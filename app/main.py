import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contextlib import asynccontextmanager

from fastapi import FastAPI

# Sentry before the app object: with SENTRY_DSN unset this whole block is a no-op
# (no SDK init, no network) — flipping one Railway var turns real error tracking on.
# Complements ops_watch (burst detector), doesn't replace it: Sentry captures each
# exception with its traceback; ops-watch owns thresholds and email alerts.
from config import SENTRY_DSN

if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=os.getenv("RAILWAY_ENVIRONMENT_NAME", "prod"),
        traces_sample_rate=0,  # errors only — no perf tracing spend
        send_default_pii=False,  # job-seeker PII must not leave our infra
    )
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.routers import (
    activity,
    admin,
    affiliate,
    applications,
    auth,
    billing,
    campaign,
    extension,
    jobs,
    profile,
    promo,
    review,
    tools,
)
from config import POOL_SWEEP_ENABLED, STALL_WATCH_ENABLED


@asynccontextmanager
async def lifespan(app_: FastAPI):
    # Email response-tracking was removed 2026-09-20. It scanned ONE shared inbox,
    # classified by subject keywords, and matched applications by company name across
    # EVERY user — so one recruiter's "unfortunately" could mark strangers' rows
    # rejected. It had been off behind EMAIL_POLL_ENABLED since 2026-06-29 and was
    # never turned back on. Its replacement is the owner marking the reply themselves
    # (PATCH /applications/{id}/status): we cannot read a user's mailbox, and the one
    # inbox we could read was never theirs.

    # Stall watch: "running, but applications aren't growing" — the failure the heartbeat
    # cannot see (app/stall_watch.py). ON by default; it only reports (activity line +
    # optional ops email), never flips a campaign flag.
    stall_task = None
    if STALL_WATCH_ENABLED:
        from app.stall_watch import watch_loop

        stall_task = asyncio.create_task(watch_loop())

    # Pool sweep: top up the job pool overnight for accounts that are actually applying,
    # so a session doesn't open on the inventory of the last one (app/pool_sweep.py).
    # Gated on activity and claimed per-account in the activity log — see the module.
    sweep_task = None
    if POOL_SWEEP_ENABLED:
        from app.pool_sweep import sweep_loop

        sweep_task = asyncio.create_task(sweep_loop())

    yield
    if stall_task:
        stall_task.cancel()
    if sweep_task:
        sweep_task.cancel()


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
app.include_router(promo.router, prefix="/api/v1")
app.include_router(billing.router, prefix="/api/v1")
app.include_router(review.router, prefix="/api/v1")
# Admin metrics: guarded by ADMIN_METRICS_TOKEN, not by a user JWT — the caller
# is the server side of the admin panel, not a signed-in HireDrop user.
app.include_router(admin.router, prefix="/api/v1")
# Affiliate applications: public apply + admin decide (same admin token).
app.include_router(affiliate.router, prefix="/api/v1")


@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}
