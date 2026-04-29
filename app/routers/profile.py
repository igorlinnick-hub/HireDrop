import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends, UploadFile, File
from fastapi.responses import JSONResponse, RedirectResponse

from app.deps import get_current_user
from app.schemas import ProfileUpdate, ConnectPlatformRequest
from app.db import profile as profile_db
from app.db import resume as resume_storage

router = APIRouter(tags=["profile"])

CONNECTABLE_PLATFORMS = ["indeed", "wellfound"]


@router.get("/profile")
def get_profile(user=Depends(get_current_user)):
    return profile_db.get_profile(user.id)


@router.post("/profile")
def update_profile(profile: ProfileUpdate, user=Depends(get_current_user)):
    updated = profile_db.update_profile(user.id, profile.dict())
    return {"message": "Profile saved", "profile": updated}


@router.post("/profile/resume")
async def upload_resume(file: UploadFile = File(...), user=Depends(get_current_user)):
    if not file.filename.lower().endswith(".pdf"):
        return JSONResponse(status_code=400, content={"error": "Only PDF files accepted"})
    contents = await file.read()
    resume_storage.upload(user.id, contents)
    return {"message": "Resume uploaded successfully", "filename": file.filename}


@router.get("/profile/resume/status")
def resume_status(user=Depends(get_current_user)):
    return {"uploaded": resume_storage.exists(user.id)}


@router.get("/profile/resume/download")
def resume_download(user=Depends(get_current_user)):
    url = resume_storage.signed_download_url(user.id)
    if not url:
        return JSONResponse(status_code=404, content={"error": "No resume uploaded"})
    return RedirectResponse(url=url, status_code=307)


@router.get("/profile/resume/url")
def resume_signed_url(user=Depends(get_current_user)):
    """Return a signed URL the extension can fetch directly without auth."""
    url = resume_storage.signed_download_url(user.id)
    if not url:
        return JSONResponse(status_code=404, content={"error": "No resume uploaded"})
    return {"url": url, "expires_in": resume_storage.SIGNED_URL_TTL_SECONDS}


@router.get("/connections")
def get_connections(user=Depends(get_current_user)):
    conns = profile_db.get_connections(user.id)
    return {
        p: {"connected": conns.get(p, {}).get("connected", False),
            "connected_at": conns.get(p, {}).get("connected_at")}
        for p in CONNECTABLE_PLATFORMS
    }


@router.post("/connections/connect")
def connect_platform(req: ConnectPlatformRequest, user=Depends(get_current_user)):
    if req.platform not in CONNECTABLE_PLATFORMS:
        return JSONResponse(
            status_code=400,
            content={"error": f"Not connectable. Supported: {CONNECTABLE_PLATFORMS}"}
        )
    profile_db.set_connection(user.id, req.platform, True)
    return {"connected": True, "platform": req.platform}


@router.post("/connections/disconnect")
def disconnect_platform(req: ConnectPlatformRequest, user=Depends(get_current_user)):
    profile_db.set_connection(user.id, req.platform, False)
    return {"disconnected": True, "platform": req.platform}
