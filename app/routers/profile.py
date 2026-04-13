import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse

from app.deps import get_current_user
from app.schemas import ProfileUpdate, ConnectPlatformRequest

router = APIRouter(tags=["profile"])

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
RESUME_PATH = os.path.join(DATA_DIR, "resume.pdf")
CONNECTIONS_PATH = os.path.join(DATA_DIR, "connections.json")

CONNECTABLE_PLATFORMS = ["indeed", "wellfound"]


@router.get("/profile")
def get_profile(user=Depends(get_current_user)):
    from data_helpers import load_profile
    profile = load_profile()
    profile.setdefault("last_name", "")
    profile.setdefault("phone", "")
    return profile


@router.post("/profile")
def update_profile(profile: ProfileUpdate, user=Depends(get_current_user)):
    import json
    from data_helpers import load_profile

    PROFILE_PATH = os.path.join(DATA_DIR, "profile.json")
    existing = load_profile()
    data = profile.dict()
    data["platform_credentials"] = existing.get("platform_credentials", {})
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PROFILE_PATH, "w") as f:
        json.dump(data, f, indent=2)
    return {"message": "Profile saved", "profile": data}


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
    from data_helpers import load_connections
    conns = load_connections()
    result = {}
    for p in CONNECTABLE_PLATFORMS:
        c = conns.get(p, {})
        result[p] = {"connected": c.get("connected", False), "connected_at": c.get("connected_at")}
    return result


@router.post("/connections/connect")
def connect_platform(req: ConnectPlatformRequest, user=Depends(get_current_user)):
    import json
    from datetime import datetime
    from data_helpers import load_connections

    if req.platform not in CONNECTABLE_PLATFORMS:
        return JSONResponse(
            status_code=400,
            content={"error": f"Platform '{req.platform}' is not connectable. Supported: {CONNECTABLE_PLATFORMS}"}
        )
    conns = load_connections()
    conns[req.platform] = {"connected": True, "connected_at": datetime.now().isoformat()}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONNECTIONS_PATH, "w") as f:
        json.dump(conns, f, indent=2)
    return {"connected": True, "platform": req.platform}


@router.post("/connections/disconnect")
def disconnect_platform(req: ConnectPlatformRequest, user=Depends(get_current_user)):
    import json
    from data_helpers import load_connections

    conns = load_connections()
    conns[req.platform] = {"connected": False, "connected_at": None}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONNECTIONS_PATH, "w") as f:
        json.dump(conns, f, indent=2)
    return {"disconnected": True, "platform": req.platform}
