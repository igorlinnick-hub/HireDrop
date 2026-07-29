import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

_EMAIL_POLL_INTERVAL = int(os.getenv("EMAIL_POLL_INTERVAL_SECONDS", "1800"))  # 30 min default


async def _email_poll_loop() -> None:
    from app.db import applications as apps_db
    from modules.email_parser import check_email_responses

    STATUS_MAP = {"interview_invite": "interview", "rejected": "rejected", "received": "received"}

    while True:
        await asyncio.sleep(_EMAIL_POLL_INTERVAL)
        try:
            for item in check_email_responses():
                company = item.get("company", "")
                new_status = STATUS_MAP.get(item["email_status"])
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
    yield
    if task:
        task.cancel()


app = FastAPI(title="HireDrop API", version="1.0.0", lifespan=lifespan)

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
