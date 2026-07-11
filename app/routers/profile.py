import io
import os
import sys
from datetime import UTC, datetime

import pdfplumber

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

from app.db import profile as profile_db
from app.db import resume as resume_storage
from app.deps import get_current_user
from app.schemas import ConnectPlatformRequest, ProfileUpdate, SearchPrefsUpdate
from modules.ats_checker import check_ats_compliance
from modules.ats_pdf_generator import (
    ATS_PASS_THRESHOLD,
    generate_ats_docx,
    generate_ats_pdf,
    get_ats_questions,
    structure_resume_data,
)

router = APIRouter(tags=["profile"])

CONNECTABLE_PLATFORMS = ["indeed", "wellfound"]


@router.get("/profile")
def get_profile(user=Depends(get_current_user)):
    return profile_db.get_profile(user.id)


@router.post("/profile")
def update_profile(profile: ProfileUpdate, user=Depends(get_current_user)):
    updated = profile_db.update_profile(user.id, profile.dict())
    return {"message": "Profile saved", "profile": updated}


@router.post("/profile/apply-mode")
def update_apply_mode(body: dict, user=Depends(get_current_user)):
    """Switch apply mode without touching the rest of the profile."""
    mode = body.get("apply_mode", "standard")
    if mode not in ("broad", "standard", "precise"):
        return JSONResponse(status_code=400, content={"error": "Invalid mode. Use: broad | standard | precise"})
    ideal = (body.get("ideal_job_description") or "").strip() or None
    profile_db.update_apply_mode(user.id, mode, ideal)
    return {"saved": True, "apply_mode": mode}


@router.post("/profile/prefs")
def update_search_prefs(prefs: SearchPrefsUpdate, user=Depends(get_current_user)):
    """Partial update — only search preferences, does not touch name/phone/etc."""
    current = profile_db.get_profile(user.id)
    payload = {
        **{k: current.get(k, "") for k in ("name", "last_name", "phone", "writing_style", "resume_url")},
        "keywords": prefs.keywords,
        "location": prefs.location,
        "job_type": prefs.job_type,
        "platforms": prefs.platforms,
    }
    updated = profile_db.update_profile(user.id, payload)
    return {"saved": True, "profile": updated}


MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10MB


@router.post("/profile/resume")
async def upload_resume(file: UploadFile = File(...), user=Depends(get_current_user)):
    if not file.filename.lower().endswith(".pdf"):
        return JSONResponse(status_code=400, content={"error": "Only PDF files accepted"})
    # Read one byte past the limit so we can detect oversize without loading
    # an unbounded body into memory (DoS guard).
    contents = await file.read(MAX_RESUME_BYTES + 1)
    if len(contents) > MAX_RESUME_BYTES:
        return JSONResponse(status_code=413, content={"error": "File too large, max 10MB"})
    # Validate it's actually a PDF — a .pdf extension is trivially spoofable.
    if not contents.startswith(b"%PDF-"):
        return JSONResponse(status_code=400, content={"error": "File is not a valid PDF"})
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


@router.post("/profile/ats/check")
async def ats_check(user=Depends(get_current_user)):
    """Download the user's current resume and run ATS compliance analysis.

    Saves results to profiles.ats_score / ats_issues / ats_checked_at.
    Returns the analysis results.
    """
    signed_url = resume_storage.signed_download_url(user.id)
    if not signed_url:
        return JSONResponse(status_code=404, content={"error": "No resume uploaded"})

    import httpx
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(signed_url, timeout=30)
            resp.raise_for_status()
            pdf_bytes = resp.content
    except Exception as e:
        print(f"[profile] resume fetch failed: {e}", file=sys.stderr)
        return JSONResponse(status_code=502, content={"error": "Could not fetch resume"})

    result = check_ats_compliance(pdf_bytes)

    # Authoritative pass/fail: a resume passes only if it has NO structural
    # design blockers AND clears the score threshold. Any design block (columns,
    # tables, images, floating blocks) forces regeneration regardless of score.
    result["passes"] = (not result["has_structural"]) and result["score"] >= ATS_PASS_THRESHOLD
    result["threshold"] = ATS_PASS_THRESHOLD

    profile_db.update_ats(user.id, {
        "ats_score": result["score"],
        "ats_issues": result["issues"],
        "ats_checked_at": datetime.now(UTC).isoformat(),
    })

    return result


@router.post("/profile/ats/questions")
async def ats_questions(user=Depends(get_current_user)):
    """Return 2-4 targeted questions about missing resume info for ATS enrichment."""
    signed_url = resume_storage.signed_download_url(user.id)
    if not signed_url:
        return JSONResponse(status_code=404, content={"error": "No resume uploaded"})

    import httpx
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(signed_url, timeout=30)
            resp.raise_for_status()
            pdf_bytes = resp.content
    except Exception as e:
        print(f"[profile] resume fetch failed: {e}", file=sys.stderr)
        return JSONResponse(status_code=502, content={"error": "Could not fetch resume"})

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        resume_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    questions = get_ats_questions(resume_text)
    return {"questions": questions}


@router.post("/profile/ats/generate")
async def ats_generate(body: dict = None, user=Depends(get_current_user)):
    """Generate an ATS-compliant PDF + DOCX from the user's current resume.

    Accepts optional body: {"answers": [{"question": "...", "answer": "..."}]}
    Answers are incorporated into the generated content to fill resume gaps.

    Stores results as resume_ats.pdf and resume_ats.docx in Supabase Storage
    (DOCX for employers/boards that don't accept PDF). The resume is structured
    once via Claude, then rendered into both formats. Returns signed preview URLs.
    """
    answers = (body or {}).get("answers") or []

    signed_url = resume_storage.signed_download_url(user.id)
    if not signed_url:
        return JSONResponse(status_code=404, content={"error": "No resume uploaded"})

    import httpx
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(signed_url, timeout=30)
            resp.raise_for_status()
            pdf_bytes = resp.content
    except Exception as e:
        print(f"[profile] resume fetch failed: {e}", file=sys.stderr)
        return JSONResponse(status_code=502, content={"error": "Could not fetch resume"})

    try:
        # Structure once → render both formats (single Claude call)
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            resume_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        data = structure_resume_data(resume_text, answers=answers)
        ats_pdf_bytes = generate_ats_pdf(data=data)
        ats_docx_bytes = generate_ats_docx(data=data)
    except Exception as e:
        print(f"[profile] resume generation failed: {e}", file=sys.stderr)
        return JSONResponse(status_code=500, content={"error": "Resume generation failed"})

    ats_path = resume_storage.upload_ats(user.id, ats_pdf_bytes)
    docx_path = None
    try:
        docx_path = resume_storage.upload_ats_docx(user.id, ats_docx_bytes)
    except Exception as e:
        # DOCX is a bonus format — don't fail the whole request if it can't store
        print(f"[ats] DOCX upload failed (non-fatal): {e}")
    profile_db.update_ats(user.id, {"ats_resume_url": ats_path})

    preview_url = resume_storage.signed_download_url_ats(user.id)
    docx_url = resume_storage.signed_download_url_ats_docx(user.id) if docx_path else None
    return {
        "success": True,
        "ats_resume_url": ats_path,
        "preview_url": preview_url,
        "docx_url": docx_url,
    }


@router.post("/profile/ats/approve")
def ats_approve(user=Depends(get_current_user)):
    """Mark the ATS-generated resume as approved for use in applications."""
    profile = profile_db.get_profile(user.id)
    if not profile.get("ats_resume_url"):
        return JSONResponse(
            status_code=400,
            content={"error": "No ATS resume generated yet. Call /profile/ats/generate first."},
        )
    profile_db.update_ats(user.id, {"ats_approved": True})
    return {"approved": True}


@router.get("/profile/ats/resume/url")
def ats_resume_url(user=Depends(get_current_user)):
    """Signed URL for the ATS-generated resume (for preview/download)."""
    url = resume_storage.signed_download_url_ats(user.id)
    if not url:
        return JSONResponse(status_code=404, content={"error": "No ATS resume generated yet"})
    return {"url": url, "expires_in": resume_storage.SIGNED_URL_TTL_SECONDS}


@router.get("/profile/ats/resume/docx-url")
def ats_resume_docx_url(user=Depends(get_current_user)):
    """Signed URL for the ATS-generated DOCX (for download)."""
    url = resume_storage.signed_download_url_ats_docx(user.id)
    if not url:
        return JSONResponse(status_code=404, content={"error": "No ATS DOCX generated yet"})
    return {"url": url, "expires_in": resume_storage.SIGNED_URL_TTL_SECONDS}


@router.post("/profile/ats/generate-from-text")
async def ats_generate_from_text(body: dict, user=Depends(get_current_user)):
    """Regenerate ATS PDF from user-edited plain text (used after in-app edit)."""
    resume_text = (body.get("resume_text") or "").strip()
    if not resume_text:
        return JSONResponse(status_code=400, content={"error": "resume_text is required"})

    try:
        data = structure_resume_data(resume_text)
        ats_pdf_bytes = generate_ats_pdf(data=data)
        ats_docx_bytes = generate_ats_docx(data=data)
    except Exception as e:
        print(f"[profile] PDF generation failed: {e}", file=sys.stderr)
        return JSONResponse(status_code=500, content={"error": "PDF generation failed"})

    ats_path = resume_storage.upload_ats(user.id, ats_pdf_bytes)
    try:
        resume_storage.upload_ats_docx(user.id, ats_docx_bytes)
    except Exception as e:
        print(f"[profile] DOCX upload failed (non-fatal): {e}", file=sys.stderr)
    profile_db.update_ats(user.id, {"ats_resume_url": ats_path})

    preview_url = resume_storage.signed_download_url_ats(user.id)
    return {"success": True, "preview_url": preview_url}


@router.get("/profile/resume/text")
async def resume_text_extract(user=Depends(get_current_user)):
    """Extract plain text from the user's uploaded PDF resume."""
    signed_url = resume_storage.signed_download_url(user.id)
    if not signed_url:
        return JSONResponse(status_code=404, content={"error": "No resume uploaded"})

    import httpx
    import pdfplumber

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(signed_url, timeout=30)
            resp.raise_for_status()
            pdf_bytes = resp.content
    except Exception as e:
        print(f"[profile] resume fetch failed: {e}", file=sys.stderr)
        return JSONResponse(status_code=502, content={"error": "Could not fetch resume"})

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    return {"text": text.strip()}


@router.post("/profile/ats/decline")
def ats_decline(user=Depends(get_current_user)):
    """User chose to keep original resume. Mark ats_approved = False."""
    profile_db.update_ats(user.id, {"ats_approved": False})
    return {"approved": False}


@router.get("/profile/resume/url/best")
def resume_best_url(job_url: str = None, user=Depends(get_current_user)):
    """Return signed URL for the best available resume.

    Priority: per-job tailored PDF (if job_url matches a scored job) →
    user-approved ATS PDF → original resume.
    """
    from app.db import jobs as jobs_db
    if job_url:
        job = jobs_db.get_by_link(user.id, job_url)
        if job and job.get("tailored_resume_pdf_url"):
            url = resume_storage.signed_url_from_path(job["tailored_resume_pdf_url"])
            if url:
                return {
                    "url": url,
                    "expires_in": resume_storage.SIGNED_URL_TTL_SECONDS,
                    "type": "tailored",
                }
    profile = profile_db.get_profile(user.id)
    ats_approved = profile.get("ats_approved") or False
    url = resume_storage.best_signed_url(user.id, ats_approved)
    if not url:
        return JSONResponse(status_code=404, content={"error": "No resume found"})
    return {
        "url": url,
        "expires_in": resume_storage.SIGNED_URL_TTL_SECONDS,
        "type": "ats" if ats_approved else "original",
    }


@router.get("/connections")
def get_connections(user=Depends(get_current_user)):
    conns = profile_db.get_connections(user.id)
    return {
        p: {
            "connected": conns.get(p, {}).get("connected", False),
            "connected_at": conns.get(p, {}).get("connected_at"),
        }
        for p in CONNECTABLE_PLATFORMS
    }


@router.post("/connections/connect")
def connect_platform(req: ConnectPlatformRequest, user=Depends(get_current_user)):
    if req.platform not in CONNECTABLE_PLATFORMS:
        return JSONResponse(
            status_code=400,
            content={"error": f"Not connectable. Supported: {CONNECTABLE_PLATFORMS}"},
        )
    profile_db.set_connection(user.id, req.platform, True)
    return {"connected": True, "platform": req.platform}


@router.post("/connections/disconnect")
def disconnect_platform(req: ConnectPlatformRequest, user=Depends(get_current_user)):
    profile_db.set_connection(user.id, req.platform, False)
    return {"disconnected": True, "platform": req.platform}
