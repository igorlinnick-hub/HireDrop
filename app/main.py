import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import (
    activity,
    applications,
    auth,
    campaign,
    extension,
    jobs,
    profile,
    tools,
)

app = FastAPI(title="HireDrop API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://hiredrop.io",
        "https://www.hiredrop.io",
        "http://localhost:3000",
    ],
    # Vercel preview deploys + chrome extension origins.
    allow_origin_regex=r"^(chrome-extension://.*|https://hiredrop-website[a-z0-9-]*\.vercel\.app)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(jobs.router, prefix="/api/v1")
app.include_router(applications.router, prefix="/api/v1")
app.include_router(campaign.router, prefix="/api/v1")
app.include_router(profile.router, prefix="/api/v1")
app.include_router(tools.router, prefix="/api/v1")
app.include_router(activity.router, prefix="/api/v1")
app.include_router(extension.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")


@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}
