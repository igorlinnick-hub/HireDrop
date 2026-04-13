import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse

from app.deps import get_current_user
from app.schemas import ProfileUpdate, ConnectPlatformRequest
from app.db import profile as profile_db

router = APIRouter(tags=["profile"])

CONNECTABLE_PLATFORMS = ["indeed", "wellfound"]
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
RESUME_PATH = os.path.join(DATA_DIR, "resume.pdf")


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
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(RESUME_PATH, "wb") as f:
        f.write(contents)
    return {"message": "Resume uploaded successfully", "filename": file.filename}


@router.get("/profile/resume/status")
def resume_status(user=Depends(get_current_user)):
    return {"uploaded": os.path.exists(RESUME_PATH)}


@router.get("/profile/resume/download")
def resume_download(user=Depends(get_current_user)):
    if not os.path.exists(RESUME_PATH):
        return JSONResponse(status_code=404, content={"error": "No resume uploaded"})
    return FileResponse(RESUME_PATH, media_type="application/pdf", filename="resume.pdf")


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
