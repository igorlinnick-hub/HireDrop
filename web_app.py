import sys
import os
import json
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
import asyncio
import json as json_lib

DAILY_LIMIT_PER_PLATFORM = 50

from database.db import init_db, save_job, get_all_jobs, get_connection, update_job_status, save_application, get_today_applications_by_platform, get_today_applications_detail
from modules.platforms import get_enabled_platforms
from modules.filters import filter_jobs
from modules.telegram_bot import send_notification
from modules.email_parser import check_email_responses
from modules.ai_cover_letter import generate_cover_letter

app = FastAPI(title="JobFlow")
init_db()

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
RESUME_PATH = os.path.join(DATA_DIR, "resume.pdf")
PROFILE_PATH = os.path.join(DATA_DIR, "profile.json")


class CoverLetterRequest(BaseModel):
    job_id: int


class AutoApplyRequest(BaseModel):
    job_id: int


class ProfileUpdate(BaseModel):
    name: str = ""
    email: str = ""
    keywords: List[str] = []
    location: str = "remote"
    job_type: str = "full-time"
    platforms: List[str] = ["remoteok"]
    writing_style: str = ""


class LetterPreviewRequest(BaseModel):
    keywords: str


class TemplateRequest(BaseModel):
    template: str


def load_profile():
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, "r") as f:
            return json.load(f)
    return {"name": "", "email": "", "keywords": ["marketing", "content", "automation"], "location": "remote", "job_type": "full-time", "platforms": ["remoteok"], "writing_style": "", "platform_credentials": {}}


def save_profile(data):
    with open(PROFILE_PATH, "w") as f:
        json.dump(data, f, indent=2)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


@app.post("/api/find-jobs")
def find_jobs():
    profile = load_profile()
    platforms = get_enabled_platforms(profile)
    all_jobs = []
    searched_platforms = []
    for platform in platforms:
        jobs = platform.scrape(
            keywords=profile.get("keywords", []),
            location=profile.get("location", "remote"),
            max_results=25
        )
        all_jobs.extend(jobs)
        searched_platforms.append(platform.display_name)

    if not all_jobs:
        return {"count": 0, "message": f"No jobs found from {', '.join(searched_platforms)}", "platforms": searched_platforms}

    filtered = filter_jobs(all_jobs)
    if not filtered:
        return {"count": 0, "message": "No new jobs matching keywords", "platforms": searched_platforms}

    for job in filtered:
        save_job(
            title=job["title"], company=job["company"], link=job["link"],
            platform=job.get("platform", "unknown"),
            description=job.get("description", ""),
            location=job.get("location", ""),
            job_type=job.get("job_type", "")
        )

    msg = f"JobFlow: {len(filtered)} new jobs found on {', '.join(searched_platforms)}!"
    send_notification(msg)
    return {"count": len(filtered), "message": f"{len(filtered)} new jobs saved", "platforms": searched_platforms}


@app.get("/api/jobs")
def get_jobs():
    jobs = get_all_jobs()
    return [
        {
            "id": job["id"],
            "title": job["title"],
            "company": job["company"],
            "link": job["link"],
            "status": job["status"],
            "date_found": job["date_found"],
            "platform": job["platform"] if "platform" in job.keys() else "remoteok",
        }
        for job in jobs
    ]


@app.post("/api/cover-letter")
def cover_letter(req: CoverLetterRequest):
    jobs = get_all_jobs()
    job = None
    for j in jobs:
        if j["id"] == req.job_id:
            job = j
            break
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    letter = generate_cover_letter({
        "title": job["title"],
        "company": job["company"],
        "description": job["description"] if "description" in job.keys() else "",
    })
    return {"letter": letter, "job_title": job["title"], "company": job["company"]}


@app.post("/api/auto-apply")
def auto_apply(req: AutoApplyRequest):
    jobs = get_all_jobs()
    job = None
    for j in jobs:
        if j["id"] == req.job_id:
            job = j
            break
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    if job["status"] == "applied":
        return JSONResponse(status_code=400, content={"error": "Already applied to this job"})

    letter = generate_cover_letter({
        "title": job["title"],
        "company": job["company"],
        "description": job["description"] if "description" in job.keys() else "",
    })

    update_job_status(job["id"], "applied")
    save_application(job["id"], letter)
    send_notification(f"JobFlow: Applied to {job['title']} at {job['company']}!")

    return {
        "message": f"Applied to {job['title']} at {job['company']}",
        "letter": letter,
        "job_title": job["title"],
        "company": job["company"],
    }


@app.get("/api/apply-stream")
async def apply_stream():
    """SSE endpoint - streams apply events in real time using Playwright."""
    from modules.applicator import apply_to_jobs

    profile = load_profile()
    jobs = get_all_jobs()
    queue = asyncio.Queue()

    async def callback(event):
        await queue.put(event)

    async def run():
        try:
            await apply_to_jobs(jobs, profile, callback)
        except Exception as e:
            await queue.put({"type": "error", "message": f"Process error: {str(e)}"})
        try:
            send_notification(f"JobFlow: Application process completed!")
        except Exception:
            pass
        await queue.put(None)

    asyncio.create_task(run())

    async def generate():
        while True:
            event = await queue.get()
            if event is None:
                break
            yield f"data: {json_lib.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class PlatformConnectRequest(BaseModel):
    platform: str


@app.post("/api/platform/open-login")
def platform_open_login(req: PlatformConnectRequest):
    """Open the platform login page in the user's system browser."""
    from modules.applicator import open_platform_login
    opened = open_platform_login(req.platform)
    return {"opened": opened, "platform": req.platform}


@app.post("/api/platform/confirm-connected")
def platform_confirm_connected(req: PlatformConnectRequest):
    """User confirms they logged in — mark platform as connected."""
    from modules.applicator import set_platform_connected
    set_platform_connected(req.platform, True)
    return {"connected": True, "platform": req.platform}


@app.post("/api/platform/disconnect")
def platform_disconnect(req: PlatformConnectRequest):
    """Mark platform as disconnected."""
    from modules.applicator import disconnect_platform as do_disconnect
    do_disconnect(req.platform)
    return {"disconnected": True, "platform": req.platform}


@app.post("/api/platform/open-verify")
def platform_open_verify(req: PlatformConnectRequest):
    """Open platform in system browser so user can check if still logged in."""
    from modules.applicator import open_platform_verify
    opened = open_platform_verify(req.platform)
    return {"opened": opened, "platform": req.platform}


@app.get("/api/platform/status")
def platform_status():
    from modules.applicator import is_platform_connected
    all_keys = [
        "remoteok", "indeed", "wellfound", "glassdoor",
        "ziprecruiter", "google_jobs", "dice",
        "toptal", "hired", "flexjobs",
    ]
    return {p: is_platform_connected(p) for p in all_keys}


@app.post("/api/cover-letter-preview")
def cover_letter_preview(req: LetterPreviewRequest):
    """Generate a sample cover letter based on current keywords."""
    profile = load_profile()
    letter = generate_cover_letter({
        "title": req.keywords or "the position",
        "company": "your company",
        "description": f"Role related to: {req.keywords}",
    }, profile)
    return {"letter": letter}


@app.post("/api/cover-letter-template")
def save_letter_template(req: TemplateRequest):
    """Save user-edited letter as the base template."""
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    with open(os.path.join(TEMPLATES_DIR, "cover_letter.txt"), "w") as f:
        f.write(req.template)
    return {"saved": True}


@app.get("/api/email-check")
def email_check():
    responses = check_email_responses()
    if responses:
        for r in responses:
            send_notification(f"JobFlow Email: {r['subject']}\nFrom: {r['sender']}")
    return {"count": len(responses), "emails": responses}


@app.get("/api/stats")
def stats():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM jobs")
    total_jobs = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM applications")
    total_applications = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM jobs WHERE date_found LIKE ?", (get_today() + "%",))
    new_today = cursor.fetchone()[0]
    conn.close()
    return {"total_jobs": total_jobs, "total_applications": total_applications, "new_today": new_today}


@app.get("/api/daily-limits")
def daily_limits():
    """Return per-platform daily application counts and limits."""
    counts = get_today_applications_by_platform()
    all_keys = [
        "remoteok", "indeed", "wellfound", "glassdoor",
        "ziprecruiter", "google_jobs", "dice",
        "toptal", "hired", "flexjobs",
    ]
    return {
        p: {"applied": counts.get(p, 0), "limit": DAILY_LIMIT_PER_PLATFORM}
        for p in all_keys
    }


@app.get("/api/campaign-history")
def campaign_history(platform: str = None):
    """Return today's applications, optionally filtered by platform."""
    apps = get_today_applications_detail(platform)
    return {"applications": apps, "count": len(apps)}


@app.post("/api/upload-resume")
async def upload_resume(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        return JSONResponse(status_code=400, content={"error": "Only PDF files accepted"})
    contents = await file.read()
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(RESUME_PATH, "wb") as f:
        f.write(contents)
    return {"message": "Resume uploaded successfully", "filename": file.filename}


@app.get("/api/resume-status")
def resume_status():
    return {"uploaded": os.path.exists(RESUME_PATH)}


@app.get("/api/profile")
def get_profile():
    return load_profile()


@app.post("/api/profile")
def update_profile(profile: ProfileUpdate):
    existing = load_profile()
    data = profile.dict()
    # Preserve platform_credentials from existing profile
    data["platform_credentials"] = existing.get("platform_credentials", {})
    os.makedirs(DATA_DIR, exist_ok=True)
    save_profile(data)
    return {"message": "Profile saved", "profile": data}


@app.get("/api/checklist")
def checklist():
    profile = load_profile()
    has_resume = os.path.exists(RESUME_PATH)
    has_keywords = len(profile.get("keywords", [])) > 0
    has_platforms = len(profile.get("platforms", [])) > 0
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM jobs")
    has_searched = cursor.fetchone()[0] > 0
    conn.close()
    return {
        "resume": has_resume,
        "keywords": has_keywords,
        "platform": has_platforms,
        "search": has_searched,
        "complete": has_resume and has_keywords and has_searched,
    }


def get_today():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JobFlow Dashboard</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/intro.js@7.2.0/minified/introjs.min.css">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0f1117;--surface:#1a1d27;--surface2:#242836;--border:#2e3348;
  --text:#e4e4e7;--text2:#9395a5;--accent:#6c5ce7;--accent2:#a78bfa;
  --green:#10b981;--red:#ef4444;--yellow:#f59e0b;--blue:#3b82f6;
}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:var(--accent2);text-decoration:none}

.introjs-tooltip{background:var(--surface)!important;color:var(--text)!important;border:1px solid var(--border)!important;border-radius:14px!important;box-shadow:0 20px 60px rgba(0,0,0,.5)!important}
.introjs-tooltiptext{color:var(--text)!important;font-size:14px!important;line-height:1.6!important}
.introjs-arrow.top{border-bottom-color:var(--surface)!important}
.introjs-arrow.bottom{border-top-color:var(--surface)!important}
.introjs-arrow.left{border-right-color:var(--surface)!important}
.introjs-arrow.right{border-left-color:var(--surface)!important}
.introjs-button{background:var(--surface2)!important;color:var(--text)!important;border:1px solid var(--border)!important;border-radius:8px!important;text-shadow:none!important;font-weight:600!important;padding:6px 16px!important}
.introjs-button:hover{background:var(--accent)!important;border-color:var(--accent)!important}
.introjs-button:focus{box-shadow:none!important}
.introjs-skipbutton{color:var(--text2)!important}
.introjs-helperLayer{border-radius:14px!important;box-shadow:0 0 0 5000px rgba(0,0,0,.6)!important}
.introjs-tooltipReferenceLayer *{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif!important}
.introjs-tooltip-header{padding-bottom:4px!important}
.introjs-tooltip-title{color:var(--accent2)!important;font-size:16px!important}
.introjs-bullets ul li a{background:var(--border)!important}
.introjs-bullets ul li a.active{background:var(--accent2)!important}
.introjs-helperNumberLayer{background:var(--accent)!important;color:#fff!important}
.introjs-progressbar{background:var(--accent)!important}
.introjs-progress{background:var(--surface2)!important}

.header{background:var(--surface);border-bottom:1px solid var(--border);padding:16px 32px;display:flex;align-items:center;justify-content:space-between}
.logo{display:flex;align-items:center;gap:12px;font-size:22px;font-weight:700}
.logo-icon{width:36px;height:36px;background:linear-gradient(135deg,var(--accent),var(--accent2));border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:18px}
.header-right{display:flex;align-items:center;gap:24px}
.header-stats{display:flex;gap:24px}
.header-stat{text-align:center}
.header-stat .val{font-size:24px;font-weight:700;color:var(--accent2)}
.header-stat .lbl{font-size:11px;color:var(--text2);text-transform:uppercase;letter-spacing:1px}
.btn-icon{width:40px;height:40px;border-radius:10px;background:var(--surface2);border:1px solid var(--border);color:var(--text);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:18px;transition:all .2s}
.btn-icon:hover{border-color:var(--accent);color:var(--accent2)}

.container{max-width:1400px;margin:0 auto;padding:24px 32px}
.grid{display:grid;grid-template-columns:1fr 340px;gap:24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:24px;margin-bottom:24px}
.card-title{font-size:16px;font-weight:600;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.card-title .dot{width:8px;height:8px;border-radius:50%}

.checklist{background:linear-gradient(135deg,rgba(108,92,231,.1),rgba(167,139,250,.05));border:1px solid rgba(108,92,231,.3);border-radius:14px;padding:24px;margin-bottom:24px}
.checklist-title{font-size:16px;font-weight:700;margin-bottom:4px;color:var(--accent2)}
.checklist-sub{font-size:13px;color:var(--text2);margin-bottom:16px}
.checklist-items{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.checklist-item{display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--surface);border:1px solid var(--border);border-radius:10px;font-size:14px;cursor:pointer;transition:all .2s}
.checklist-item:hover{border-color:var(--accent)}
.checklist-item.done{opacity:.6}
.check-icon{width:22px;height:22px;border-radius:6px;border:2px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0}
.checklist-item.done .check-icon{background:var(--green);border-color:var(--green);color:#fff}
.checklist-progress{margin-top:14px;height:4px;background:var(--surface2);border-radius:2px;overflow:hidden}
.checklist-progress-bar{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:2px;transition:width .4s}

.action-bar{display:none}
.btn{padding:12px 28px;border:none;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s}
.btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(108,92,231,.4)}
.btn-primary:disabled{opacity:.5;cursor:not-allowed;transform:none;box-shadow:none}
.btn-secondary{background:var(--surface2);color:var(--text);border:1px solid var(--border)}
.btn-secondary:hover{border-color:var(--accent)}
.btn-sm{padding:6px 14px;font-size:12px;border-radius:8px}
.btn-green{background:var(--green);color:#fff;border:none}
.btn-green:hover{opacity:.9}
.btn-blue{background:var(--blue);color:#fff;border:none}
.btn-blue:hover{opacity:.9}

.resume-section{display:flex;align-items:center;gap:12px}
.resume-badge{display:flex;align-items:center;gap:6px;padding:6px 12px;border-radius:8px;font-size:12px;font-weight:600}
.resume-badge.uploaded{background:rgba(16,185,129,.15);color:var(--green)}
.resume-badge.missing{background:rgba(239,68,68,.15);color:var(--red)}
input[type="file"]{display:none}

.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:10px 14px;font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--text2);border-bottom:1px solid var(--border)}
td{padding:12px 14px;border-bottom:1px solid var(--border);font-size:14px}
tr:hover td{background:var(--surface2)}
.status{padding:4px 10px;border-radius:6px;font-size:12px;font-weight:600}
.status-new{background:rgba(16,185,129,.15);color:var(--green)}
.status-applied{background:rgba(59,130,246,.15);color:var(--blue)}
.status-interview{background:rgba(245,158,11,.15);color:var(--yellow)}
.status-response{background:rgba(139,92,246,.15);color:#a78bfa}
.status-rejected{background:rgba(239,68,68,.15);color:#f87171}
.btn-view{padding:4px 10px;border-radius:6px;font-size:11px;font-weight:600;background:var(--surface2);color:var(--muted);border:1px solid var(--border);cursor:pointer;transition:all .2s;text-decoration:none;display:inline-flex;align-items:center;gap:3px}
.btn-view:hover{color:var(--text);border-color:var(--accent)}
.job-filter-tabs{display:flex;gap:6px;margin-bottom:14px}
.job-filter-tab{padding:5px 12px;border-radius:6px;font-size:12px;font-weight:600;background:var(--surface2);color:var(--muted);border:1px solid transparent;cursor:pointer;transition:all .2s}
.job-filter-tab:hover{color:var(--text)}
.job-filter-tab.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.platform-tag{padding:3px 8px;border-radius:6px;font-size:11px;font-weight:600;background:rgba(108,92,231,.15);color:var(--accent2)}

.platform-list{display:flex;flex-direction:column;gap:10px}
.platform{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:var(--surface2);border-radius:10px;cursor:pointer;transition:all .2s}
.platform:hover{border-color:var(--accent)}
.platform-info{display:flex;align-items:center;gap:10px}
.platform-dot{width:10px;height:10px;border-radius:50%}
.platform-dot.active{background:var(--green)}
.platform-dot.inactive{background:var(--border)}
.platform-name{font-size:14px;font-weight:500}
.platform-badge{font-size:11px;padding:3px 8px;border-radius:6px;font-weight:600}
.badge-connected{background:rgba(16,185,129,.15);color:var(--green)}
.badge-off{background:rgba(147,149,165,.1);color:var(--text2)}

/* Toggle Switch */
.toggle{position:relative;width:44px;height:24px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:var(--border);border-radius:12px;transition:.3s}
.toggle-slider:before{position:absolute;content:"";height:18px;width:18px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.3s}
.toggle input:checked+.toggle-slider{background:var(--green)}
.toggle input:checked+.toggle-slider:before{transform:translateX(20px)}

.log{display:flex;flex-direction:column;gap:8px;max-height:300px;overflow-y:auto}
.log-item{display:flex;align-items:flex-start;gap:10px;padding:8px 12px;background:var(--surface2);border-radius:8px;font-size:13px}
.log-time{color:var(--text2);font-size:11px;white-space:nowrap;min-width:55px}
.log-msg{color:var(--text)}

.email-list{display:flex;flex-direction:column;gap:8px}
.email-item{padding:12px;background:var(--surface2);border-radius:10px}
.email-subject{font-weight:600;font-size:14px;margin-bottom:4px}
.email-meta{font-size:12px;color:var(--text2)}

.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.6);z-index:100;align-items:center;justify-content:center}
.modal-overlay.active{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:32px;max-width:640px;width:90%;max-height:80vh;overflow-y:auto}
.modal-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
.modal-close{background:none;border:none;color:var(--text2);font-size:24px;cursor:pointer}
.modal-close:hover{color:var(--text)}
.letter-content{white-space:pre-wrap;line-height:1.7;font-size:14px;background:var(--surface2);padding:20px;border-radius:10px}

.settings-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.4);z-index:200}
.settings-overlay.active{display:block}
.settings-panel{position:fixed;top:0;right:-440px;width:440px;height:100%;background:var(--surface);border-left:1px solid var(--border);z-index:201;transition:right .3s ease;overflow-y:auto;padding:32px}
.settings-panel.active{right:0}
.settings-title{font-size:20px;font-weight:700;margin-bottom:24px;display:flex;align-items:center;justify-content:space-between}
.form-group{margin-bottom:20px}
.form-label{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:var(--text2);margin-bottom:8px;display:block}
.form-input{width:100%;padding:10px 14px;background:var(--surface2);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:14px;outline:none}
.form-input:focus{border-color:var(--accent)}
.form-select{width:100%;padding:10px 14px;background:var(--surface2);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:14px;outline:none;-webkit-appearance:none}
.form-select:focus{border-color:var(--accent)}
.form-section{font-size:14px;font-weight:600;color:var(--accent2);margin:28px 0 16px;padding-top:20px;border-top:1px solid var(--border)}

.tags-container{display:flex;flex-wrap:wrap;gap:8px;padding:10px 14px;background:var(--surface2);border:1px solid var(--border);border-radius:10px;min-height:44px;cursor:text}
.tags-container:focus-within{border-color:var(--accent)}
.tag{display:flex;align-items:center;gap:4px;padding:4px 10px;background:rgba(108,92,231,.2);border:1px solid rgba(108,92,231,.3);border-radius:6px;font-size:13px;color:var(--accent2)}
.tag-remove{cursor:pointer;font-size:16px;line-height:1;opacity:.7}
.tag-remove:hover{opacity:1}
.tag-input{border:none;background:none;color:var(--text);font-size:14px;outline:none;min-width:80px;flex:1}
.tag-input::placeholder{color:var(--text2)}

.platform-toggle{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:var(--surface2);border:1px solid var(--border);border-radius:10px;margin-bottom:8px}
.platform-toggle-name{font-size:14px;font-weight:500}

.spinner{display:inline-block;width:16px;height:16px;border:2px solid var(--border);border-top-color:var(--accent2);border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle;margin-right:8px}
@keyframes spin{to{transform:rotate(360deg)}}

.empty{text-align:center;padding:40px;color:var(--text2)}

::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}

.btn-actions{display:flex;gap:6px}

/* Filter Bar */
.filter-bar{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px 24px;margin-bottom:24px}
.filter-bar-title{font-size:14px;font-weight:600;margin-bottom:14px;color:var(--accent2);display:flex;align-items:center;gap:8px}
.filter-row{display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap}
.filter-group{display:flex;flex-direction:column;gap:6px}
.filter-group label{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--text2)}
.filter-group select{padding:8px 12px;background:var(--surface2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;outline:none;min-width:140px}
.filter-group select:focus{border-color:var(--accent)}
.filter-tags{display:flex;flex-wrap:wrap;gap:6px;padding:8px 12px;background:var(--surface2);border:1px solid var(--border);border-radius:8px;min-height:38px;min-width:220px;cursor:text;align-items:center}
.filter-tags:focus-within{border-color:var(--accent)}
.filter-tags .tag{font-size:12px;padding:2px 8px}
.filter-tags input{border:none;background:none;color:var(--text);font-size:13px;outline:none;min-width:60px;flex:1}
.filter-tags input::placeholder{color:var(--text2)}
.filter-platforms{display:flex;gap:8px;align-items:center}
.filter-plt{display:flex;align-items:center;gap:6px;padding:6px 12px;background:var(--surface2);border:1px solid var(--border);border-radius:8px;font-size:12px;cursor:pointer;transition:all .2s;user-select:none;position:relative}
.filter-plt:hover{border-color:var(--accent)}
.filter-plt.active{background:rgba(108,92,231,.2);border-color:var(--accent);color:var(--accent2)}
.filter-plt.disconnected{opacity:.7}
.filter-plt .plt-status{font-size:10px;line-height:1}.filter-plt.active .plt-status{color:var(--green)}
.filter-plt .plt-connect-btn{font-size:10px;padding:2px 6px;background:var(--accent);color:#fff;border:none;border-radius:4px;cursor:pointer;margin-left:2px;white-space:nowrap}
.filter-plt .plt-connect-btn:hover{opacity:.85}
.filter-plt input{display:none}
.filter-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}

/* Campaign Fullscreen Modal */
.campaign-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);z-index:300;align-items:center;justify-content:center}
.campaign-overlay.active{display:flex}
.campaign-modal{background:#0d1117;border:1px solid var(--border);border-radius:16px;width:94vw;max-width:900px;height:85vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 24px 80px rgba(0,0,0,.6)}
.campaign-header{padding:20px 28px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:var(--surface);flex-shrink:0}
.campaign-header-left{display:flex;align-items:center;gap:12px}
.campaign-dot{width:10px;height:10px;border-radius:50%;background:var(--green)}
.campaign-dot.pulsing{animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.campaign-title{font-size:20px;font-weight:700}

/* Campaign Cards */
.campaign-cards{display:flex;gap:14px;padding:20px 28px;border-bottom:1px solid var(--border);background:var(--surface);flex-shrink:0}
.campaign-card{flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:16px 18px;cursor:pointer;transition:all .2s;min-width:0}
.campaign-card:hover{border-color:var(--accent)}
.campaign-card.selected{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.campaign-card-name{font-size:15px;font-weight:600;margin-bottom:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.campaign-bar-wrap{height:8px;background:var(--bg);border-radius:4px;overflow:hidden;margin-bottom:8px}
.campaign-bar-fill{height:100%;border-radius:4px;transition:width .4s}
.campaign-bar-fill.running{background:linear-gradient(90deg,var(--green),var(--accent2))}
.campaign-bar-fill.waiting{background:var(--border)}
.campaign-bar-fill.done{background:var(--green)}
.campaign-bar-fill.failed{background:var(--red)}
.campaign-bar-fill.limit{background:var(--yellow)}
.campaign-count{font-size:13px;color:var(--text2);font-weight:600;margin-bottom:6px;font-variant-numeric:tabular-nums}
.campaign-status{font-size:12px;display:flex;align-items:center;gap:6px}
.campaign-status.waiting{color:var(--text2)}
.campaign-status.running{color:var(--green)}
.campaign-status.done{color:var(--green)}
.campaign-status.failed{color:var(--red)}
.campaign-status.limit{color:var(--yellow)}

/* Campaign body: log + history */
.campaign-body{flex:1;overflow:hidden;display:flex;flex-direction:column;min-height:0}
.campaign-view{flex:1;overflow-y:auto;display:none;flex-direction:column}
.campaign-view.active{display:flex}
.campaign-log{flex:1;overflow-y:auto;padding:20px 28px;font-family:'SF Mono',SFMono-Regular,Consolas,'Liberation Mono',Menlo,monospace;font-size:13px;line-height:2}
.campaign-log-line{padding:2px 0}
.campaign-log-line.platform-header{color:var(--blue);font-weight:600;margin-top:10px}
.campaign-log-line.generating{color:var(--text2)}
.campaign-log-line.applied{color:var(--green)}
.campaign-log-line.error{color:var(--red)}
.campaign-log-line.done{color:var(--yellow);font-weight:600;margin-top:10px}

/* History list */
.history-header{padding:16px 28px;font-size:15px;font-weight:600;color:var(--accent2);border-bottom:1px solid var(--border);flex-shrink:0}
.history-list{flex:1;overflow-y:auto;padding:8px 28px}
.history-item{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;border-bottom:1px solid var(--border);transition:background .15s}
.history-item:hover{background:var(--surface2)}
.history-item-left{display:flex;align-items:center;gap:10px;min-width:0;flex:1}
.history-item-check{color:var(--green);font-size:16px;flex-shrink:0}
.history-item-info{min-width:0}
.history-item-title{font-size:14px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.history-item-company{font-size:12px;color:var(--text2)}
.history-item-link{color:var(--accent2);font-size:13px;text-decoration:none;white-space:nowrap;flex-shrink:0}
.history-item-link:hover{text-decoration:underline}

.campaign-footer{padding:16px 28px;border-top:1px solid var(--border);background:var(--surface);display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
.campaign-footer-info{font-size:13px;color:var(--text2)}

/* Responsive: grid + sidebar + campaign cards */
@media(max-width:1100px){
  .grid{grid-template-columns:1fr}
  .container{padding:16px}
}
@media(max-width:768px){
  .header{padding:12px 16px;flex-wrap:wrap;gap:12px}
  .header-stats{gap:12px}
  .header-stat .val{font-size:18px}
  .filter-row{flex-direction:column;gap:12px}
  .filter-actions{width:100%}
  .campaign-cards{flex-direction:column}
  .campaign-modal{width:100%;height:100%;max-width:100%;border-radius:0}
  .settings-panel{width:100%}
}

/* Platform sidebar: prevent overflow */
.platform{min-width:0;overflow:hidden}
.platform-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.platform-info{min-width:0;overflow:hidden}
</style>
</head>
<body>

<div class="header">
  <div class="logo" data-intro="Welcome to JobFlow! Your AI-powered job search dashboard. Let's take a quick tour." data-step="1" data-title="Welcome to JobFlow">
    <div class="logo-icon">JF</div>
    JobFlow
  </div>
  <div class="header-right">
    <div class="header-stats">
      <div class="header-stat">
        <div class="val" id="stat-total">-</div>
        <div class="lbl">Total Jobs</div>
      </div>
      <div class="header-stat">
        <div class="val" id="stat-today">-</div>
        <div class="lbl">New Today</div>
      </div>
      <div class="header-stat">
        <div class="val" id="stat-applied">-</div>
        <div class="lbl">Applied</div>
      </div>
      <div class="header-stat">
        <div class="val" id="stat-emails">-</div>
        <div class="lbl">Responses</div>
      </div>
    </div>
    <button class="btn-icon" onclick="openSettings()" title="Settings">&#9881;</button>
  </div>
</div>

<div class="container">
  <div class="checklist" id="setup-checklist" style="display:none">
    <div class="checklist-title">Complete Your Setup</div>
    <div class="checklist-sub">Finish these steps to get the most out of JobFlow</div>
    <div class="checklist-items">
      <div class="checklist-item" id="cl-resume" onclick="document.getElementById('resume-input').click()" data-intro="Upload your resume (PDF) so JobFlow can tailor cover letters to your experience." data-step="2" data-title="Upload Resume">
        <span class="check-icon">&#10003;</span>
        <span>Upload Resume</span>
      </div>
      <div class="checklist-item" id="cl-keywords" onclick="openSettings()" data-intro="Set your search keywords, location, and job type preferences to find the right opportunities." data-step="3" data-title="Set Your Filters">
        <span class="check-icon">&#10003;</span>
        <span>Set Keywords</span>
      </div>
      <div class="checklist-item" id="cl-platform" onclick="openSettings()" data-intro="Enable the job platforms you want to search." data-step="4" data-title="Choose Platforms">
        <span class="check-icon">&#10003;</span>
        <span>Enable Platforms</span>
      </div>
      <div class="checklist-item" id="cl-search" onclick="findJobs()" data-intro="Click Find Jobs to search all enabled platforms for matching opportunities." data-step="5" data-title="Find Jobs">
        <span class="check-icon">&#10003;</span>
        <span>Start First Search</span>
      </div>
    </div>
    <div class="checklist-progress"><div class="checklist-progress-bar" id="cl-progress" style="width:25%"></div></div>
  </div>

  <!-- Filter Bar -->
  <div class="filter-bar" data-intro="Set your filters and search across all platforms at once." data-step="6" data-title="Filter & Search">
    <div class="filter-bar-title">Search Filters</div>
    <div class="filter-row">
      <div class="filter-group">
        <label>Keywords</label>
        <div class="filter-tags" id="filter-tags" onclick="document.getElementById('filter-tag-input').focus()">
          <input id="filter-tag-input" type="text" placeholder="Add keyword...">
        </div>
      </div>
      <div class="filter-group">
        <label>Location</label>
        <select id="filter-location">
          <option value="remote">Remote</option>
          <option value="usa">USA</option>
          <option value="europe">Europe</option>
          <option value="">Any</option>
        </select>
      </div>
      <div class="filter-group">
        <label>Job Type</label>
        <select id="filter-jobtype">
          <option value="full-time">Full-time</option>
          <option value="part-time">Part-time</option>
          <option value="contract">Contract</option>
        </select>
      </div>
      <div class="filter-group">
        <label>Platforms</label>
        <div class="filter-platforms" id="filter-platforms"></div>
      </div>
      <div class="filter-actions">
        <button class="btn btn-primary" id="btn-find" onclick="findJobsWithFilters()">Find Jobs</button>
        <button class="btn btn-green" id="btn-apply-all" onclick="openApplyPanel()">Start Campaign</button>
        <button class="btn btn-secondary btn-sm" onclick="refreshJobs()">Refresh</button>
        <button class="btn btn-secondary btn-sm" onclick="checkEmails()">Emails</button>
        <label class="btn btn-secondary btn-sm" for="resume-input" id="resume-upload-btn" style="cursor:pointer;margin:0">Resume</label>
        <input type="file" id="resume-input" accept=".pdf" onchange="uploadResume(this)">
        <span id="resume-status"></span>
        <span id="action-status" style="color:var(--text2);font-size:13px"></span>
      </div>
    </div>
  </div>

  <!-- Cover Letter Preview -->
  <div class="card" id="cover-letter-preview-card">
    <div class="card-title" style="justify-content:space-between">
      <span><span class="dot" style="background:var(--accent2)"></span> Cover Letter Preview</span>
      <button class="btn btn-secondary btn-sm" onclick="regenerateLetter()" id="regen-btn">Regenerate</button>
    </div>
    <textarea id="cover-letter-preview" rows="6" style="width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:14px;color:var(--text);font-size:13px;line-height:1.7;resize:vertical;font-family:inherit" placeholder="Fill in keywords above and click Regenerate to generate your cover letter..."></textarea>
    <div id="cover-letter-resume-warning" style="display:none;margin-top:10px;padding:10px 14px;background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.3);border-radius:8px;font-size:13px;color:var(--yellow)">
      &#9888; Upload your resume for a personalized letter — <label for="resume-input" style="color:var(--accent2);cursor:pointer;text-decoration:underline">Upload Resume</label>
    </div>
    <div style="margin-top:10px;display:flex;align-items:center;justify-content:space-between">
      <div style="font-size:12px;color:var(--text2)"><strong>Edit this to sound like YOU.</strong> Each letter will be slightly customized per job automatically.</div>
      <button class="btn btn-secondary btn-sm" onclick="saveLetterTemplate()" id="save-letter-btn">Save as Template</button>
    </div>
  </div>

  <div class="grid">
    <div>
      <div class="card">
        <div class="card-title"><span class="dot" style="background:var(--green)"></span>Job Listings</div>
        <div class="job-filter-tabs" id="job-filter-tabs"></div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th>Title</th><th>Company</th><th>Platform</th><th>Date</th><th>Status</th><th>Actions</th></tr>
            </thead>
            <tbody id="jobs-table"><tr><td colspan="6" class="empty">No jobs yet. Click "Find Jobs" to start.</td></tr></tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><span class="dot" style="background:var(--blue)"></span>Email Responses</div>
        <div class="email-list" id="email-list">
          <div class="empty">No email responses checked yet.</div>
        </div>
      </div>
    </div>

    <div>
      <div class="card" id="platforms-card">
        <div class="card-title"><span class="dot" style="background:var(--accent)"></span>Platforms</div>
        <div class="platform-list" id="platform-list"></div>
        <div class="platform-list" id="platform-list-sidebar" style="margin-top:12px;border-top:1px solid var(--border);padding-top:12px"></div>
      </div>

      <div class="card">
        <div class="card-title"><span class="dot" style="background:var(--yellow)"></span>Activity Log</div>
        <div class="log" id="activity-log">
          <div class="log-item"><span class="log-time">now</span><span class="log-msg">Dashboard loaded</span></div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Cover Letter Modal -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <div class="modal-header">
      <h3 id="modal-title">Cover Letter</h3>
      <button class="modal-close" onclick="closeModal()">&times;</button>
    </div>
    <div class="letter-content" id="modal-body">Loading...</div>
    <div style="margin-top:16px;text-align:right">
      <button class="btn btn-secondary btn-sm" onclick="copyLetter()">Copy to Clipboard</button>
    </div>
  </div>
</div>

<!-- Settings Panel -->
<div class="settings-overlay" id="settings-overlay" onclick="closeSettings()"></div>
<div class="settings-panel" id="settings-panel">
  <div class="settings-title">
    <span>Settings</span>
    <button class="modal-close" onclick="closeSettings()">&times;</button>
  </div>

  <div class="form-section" style="margin-top:0;border-top:none;padding-top:0">Profile</div>
  <div class="form-group">
    <label class="form-label">Full Name</label>
    <input class="form-input" id="set-name" type="text" placeholder="Your name">
  </div>
  <div class="form-group">
    <label class="form-label">Email Address</label>
    <input class="form-input" id="set-email" type="email" placeholder="you@example.com">
  </div>

  <div class="form-section">Search Filters</div>
  <div class="form-group">
    <label class="form-label">Keywords</label>
    <div class="tags-container" id="tags-container" onclick="document.getElementById('tag-input').focus()">
      <input class="tag-input" id="tag-input" type="text" placeholder="Type and press Enter">
    </div>
  </div>
  <div class="form-group">
    <label class="form-label">Location Preference</label>
    <select class="form-select" id="set-location">
      <option value="remote">Remote</option>
      <option value="hybrid">Hybrid</option>
      <option value="onsite">On-site</option>
    </select>
  </div>
  <div class="form-group">
    <label class="form-label">Job Type</label>
    <select class="form-select" id="set-jobtype">
      <option value="full-time">Full-time</option>
      <option value="part-time">Part-time</option>
      <option value="contract">Contract</option>
    </select>
  </div>

  <div class="form-section">Your Writing Style</div>
  <div class="form-group">
    <label class="form-label">Write 2-3 sentences in your own voice. The AI will match your tone.</label>
    <textarea id="set-writing-style" class="form-input" rows="4" placeholder="Example: Hey, I'm Igor. I've spent the last 3 years building marketing systems that actually work. I hate fluff and I get things done." style="resize:vertical"></textarea>
    <div style="font-size:11px;color:var(--text2);margin-top:6px">The more natural and specific, the better. Just be yourself.</div>
  </div>

  <div class="form-section">Platforms</div>
  <div class="form-group" id="settings-platforms"></div>

  <div class="form-section">Resume</div>
  <div class="form-group">
    <label class="btn btn-secondary" for="resume-input-panel" style="display:inline-block;cursor:pointer">Choose PDF File</label>
    <input type="file" id="resume-input-panel" accept=".pdf" onchange="uploadResume(this)" style="display:none">
    <span id="resume-status-panel" style="margin-left:10px;font-size:13px;color:var(--text2)"></span>
  </div>

  <button class="btn btn-green" onclick="saveSettings()" style="width:100%;margin-top:12px">Save Settings</button>
</div>

<!-- Connect Platform Modal -->
<div class="modal-overlay" id="connect-modal">
  <div class="modal" style="max-width:440px">
    <div class="modal-header">
      <h3 id="connect-modal-title">Connect Platform</h3>
      <button class="modal-close" onclick="closeConnectModal()">&times;</button>
    </div>
    <!-- Step 1: Open browser -->
    <div id="connect-step1">
      <p style="font-size:13px;color:var(--text2);margin-bottom:16px">Your default browser (Safari/Chrome) will open the login page. Log in with your saved passwords — we never see or store your credentials.</p>
      <div style="display:flex;gap:10px;justify-content:flex-end">
        <button class="btn btn-secondary" onclick="closeConnectModal()">Cancel</button>
        <button class="btn btn-primary" onclick="connectStep1()">Open Login Page</button>
      </div>
    </div>
    <!-- Step 2: Confirm -->
    <div id="connect-step2" style="display:none">
      <div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px">
        <div style="font-size:14px;font-weight:600;margin-bottom:8px">Login page opened in your browser</div>
        <div style="font-size:13px;color:var(--text2)">Log in to <span id="connect-platform-name" style="color:var(--accent2);font-weight:600"></span>, then come back here and confirm.</div>
      </div>
      <div style="display:flex;gap:10px;justify-content:flex-end">
        <button class="btn btn-secondary" onclick="closeConnectModal()">Cancel</button>
        <button class="btn btn-green" onclick="connectConfirm()">I'm Connected</button>
      </div>
    </div>
  </div>
</div>

<!-- Verify Connection Modal -->
<div class="modal-overlay" id="verify-modal">
  <div class="modal" style="max-width:400px">
    <div class="modal-header">
      <h3 id="verify-modal-title">Verify Connection</h3>
      <button class="modal-close" onclick="closeVerifyModal()">&times;</button>
    </div>
    <p style="font-size:13px;color:var(--text2);margin-bottom:16px">We opened <span id="verify-platform-name" style="color:var(--accent2);font-weight:600"></span> in your browser. Are you still logged in?</p>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn btn-secondary" onclick="verifyNo()" style="color:var(--red)">No, Disconnect</button>
      <button class="btn btn-green" onclick="verifyYes()">Yes, Still Connected</button>
    </div>
  </div>
</div>

<!-- Campaign Modal (fullscreen) -->
<div class="campaign-overlay" id="campaign-overlay">
  <div class="campaign-modal">
    <div class="campaign-header">
      <div class="campaign-header-left">
        <div class="campaign-dot" id="campaign-dot"></div>
        <span class="campaign-title">Campaign</span>
      </div>
      <button class="modal-close" onclick="closeCampaign()">&times;</button>
    </div>
    <div class="campaign-cards" id="campaign-cards"></div>
    <div class="campaign-body">
      <div class="campaign-view active" id="campaign-view-log">
        <div class="campaign-log" id="campaign-log"></div>
      </div>
      <div class="campaign-view" id="campaign-view-history">
        <div class="history-header" id="history-header"></div>
        <div class="history-list" id="history-list"></div>
      </div>
    </div>
    <div class="campaign-footer">
      <span class="campaign-footer-info" id="campaign-footer-info"></span>
      <button class="btn btn-secondary btn-sm" onclick="closeCampaign()">Close</button>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/intro.js@7.2.0/intro.min.js"></script>
<script>
const statusEl = document.getElementById('action-status');
const logEl = document.getElementById('activity-log');
let currentTags = [];
let filterTags = [];
let currentPlatforms = ['remoteok'];
const ALL_PLATFORMS = {
  remoteok:'RemoteOK', indeed:'Indeed', wellfound:'Wellfound',
  glassdoor:'Glassdoor', ziprecruiter:'ZipRecruiter', google_jobs:'Google Jobs', dice:'Dice',
  toptal:'Toptal', hired:'Hired', flexjobs:'FlexJobs'
};
const FREE_PLATFORMS = ['remoteok','indeed','wellfound','glassdoor','ziprecruiter','google_jobs','dice'];
const PAID_PLATFORMS = ['toptal','hired','flexjobs'];
const PAID_URLS = {
  toptal:'https://www.toptal.com', hired:'https://hired.com', flexjobs:'https://www.flexjobs.com'
};
let platformConnected = {};

function log(msg) {
  const now = new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  const item = document.createElement('div');
  item.className = 'log-item';
  item.innerHTML = '<span class="log-time">' + now + '</span><span class="log-msg">' + escapeHtml(msg) + '</span>';
  logEl.prepend(item);
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function setStatus(msg, loading) {
  statusEl.innerHTML = (loading ? '<span class="spinner"></span>' : '') + escapeHtml(msg);
}

// Platforms display — show enabled platforms for search
function renderPlatforms() {
  const el = document.getElementById('platform-list');
  const connectedKeys = Object.entries(platformConnected).filter(([k,v]) => v).map(([k]) => k);
  if (!connectedKeys.length) {
    el.innerHTML = '<div style="text-align:center;color:var(--text2);font-size:13px;padding:12px">Connect platforms in the sidebar below</div>';
    return;
  }
  el.innerHTML = connectedKeys.map(key => {
    const name = ALL_PLATFORMS[key] || key;
    const active = currentPlatforms.includes(key);
    return '<div class="platform" onclick="togglePlatform(&#39;' + key + '&#39;)">' +
      '<div class="platform-info"><div class="platform-dot ' + (active ? 'active' : 'inactive') + '"></div><span class="platform-name">' + name + '</span></div>' +
      '<span class="platform-badge ' + (active ? 'badge-connected' : 'badge-off') + '">' + (active ? 'Enabled' : 'Disabled') + '</span>' +
    '</div>';
  }).join('');
}

function togglePlatform(key) {
  if (currentPlatforms.includes(key)) {
    if (currentPlatforms.length > 1) currentPlatforms = currentPlatforms.filter(p => p !== key);
  } else {
    currentPlatforms.push(key);
  }
  renderPlatforms();
  updatePlatformToggles();
}

function updatePlatformToggles() {
  // Render settings platform toggles dynamically
  const container = document.getElementById('settings-platforms');
  if (container) {
    let html = '<div style="font-size:11px;color:var(--green);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Free</div>';
    FREE_PLATFORMS.forEach(key => {
      const checked = currentPlatforms.includes(key) ? ' checked' : '';
      html += '<div class="platform-toggle"><span class="platform-toggle-name">' + ALL_PLATFORMS[key] + '</span><label class="toggle"><input type="checkbox" id="plt-' + key + '" value="' + key + '"' + checked + '><span class="toggle-slider"></span></label></div>';
    });
    html += '<div style="font-size:11px;color:var(--yellow);text-transform:uppercase;letter-spacing:1px;margin:14px 0 8px">Premium</div>';
    PAID_PLATFORMS.forEach(key => {
      const checked = currentPlatforms.includes(key) ? ' checked' : '';
      html += '<div class="platform-toggle"><span class="platform-toggle-name">' + ALL_PLATFORMS[key] + '</span><label class="toggle"><input type="checkbox" id="plt-' + key + '" value="' + key + '"' + checked + '><span class="toggle-slider"></span></label></div>';
    });
    container.innerHTML = html;
  }
}

async function loadStats() {
  try {
    const res = await fetch('/api/stats');
    const data = await res.json();
    document.getElementById('stat-total').textContent = data.total_jobs;
    document.getElementById('stat-today').textContent = data.new_today;
    document.getElementById('stat-applied').textContent = data.total_applications;
  } catch(e) {}
}

async function findJobs() {
  const btn = document.getElementById('btn-find');
  btn.disabled = true;
  const names = currentPlatforms.map(p => ALL_PLATFORMS[p]).join(', ');
  setStatus('Searching ' + names + '...', true);
  log('Started job search on ' + names);
  try {
    const res = await fetch('/api/find-jobs', {method:'POST'});
    const data = await res.json();
    setStatus(data.message, false);
    log(data.message);
    await refreshJobs();
    await loadStats();
    await loadChecklist();
  } catch(e) {
    setStatus('Error: ' + e.message, false);
    log('Search failed: ' + e.message);
  }
  btn.disabled = false;
}

let allJobs = [];
let jobFilterStatus = 'all';
const STATUS_ICONS = {new:'🟢',applied:'✅',interview:'📋',response:'📧',rejected:'❌'};

function renderJobFilterTabs() {
  const counts = {all:allJobs.length, new:0, applied:0, response:0};
  allJobs.forEach(j => { if (counts[j.status] !== undefined) counts[j.status]++; else if (j.status === 'interview') counts.response = (counts.response||0); });
  // count responses+interview together
  counts.response = allJobs.filter(j => j.status === 'response' || j.status === 'interview').length;
  const tabs = [
    {key:'all', label:'All'},
    {key:'new', label:'New'},
    {key:'applied', label:'Applied'},
    {key:'response', label:'Responses'},
  ];
  document.getElementById('job-filter-tabs').innerHTML = tabs.map(t =>
    '<button class="job-filter-tab' + (jobFilterStatus===t.key?' active':'') + '" onclick="setJobFilter(&#39;' + t.key + '&#39;)">' + t.label + ' (' + (counts[t.key]||0) + ')</button>'
  ).join('');
}

function setJobFilter(status) {
  jobFilterStatus = status;
  renderJobFilterTabs();
  renderJobRows();
}

function renderJobRows() {
  const tbody = document.getElementById('jobs-table');
  const filtered = jobFilterStatus === 'all' ? allJobs
    : jobFilterStatus === 'response' ? allJobs.filter(j => j.status === 'response' || j.status === 'interview')
    : allJobs.filter(j => j.status === jobFilterStatus);
  if (!filtered.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">No jobs in this filter.</td></tr>';
    return;
  }
  tbody.innerHTML = filtered.map(j => {
    const pName = ALL_PLATFORMS[j.platform] || j.platform;
    const icon = STATUS_ICONS[j.status] || '';
    const dateStr = j.date_found ? j.date_found.slice(5,10) : '';
    const viewBtn = j.link ? '<a href="' + escapeHtml(j.link) + '" target="_blank" class="btn-view">View &#8599;</a>' : '';
    return '<tr>' +
      '<td>' + escapeHtml(j.title) + '</td>' +
      '<td>' + escapeHtml(j.company) + '</td>' +
      '<td><span class="platform-tag">' + escapeHtml(pName) + '</span></td>' +
      '<td>' + dateStr + '</td>' +
      '<td><span class="status status-' + j.status + '">' + icon + ' ' + j.status + '</span></td>' +
      '<td>' + viewBtn + '</td>' +
    '</tr>';
  }).join('');
}

async function refreshJobs() {
  try {
    const res = await fetch('/api/jobs');
    allJobs = await res.json();
    const tbody = document.getElementById('jobs-table');
    if (!allJobs.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">No jobs yet. Click "Find Jobs" to start.</td></tr>';
      document.getElementById('job-filter-tabs').innerHTML = '';
      return;
    }
    renderJobFilterTabs();
    renderJobRows();
  } catch(e) {}
}

async function genCoverLetter(jobId) {
  document.getElementById('modal').classList.add('active');
  document.getElementById('modal-body').textContent = 'Generating with AI...';
  document.getElementById('modal-title').textContent = 'Cover Letter';
  try {
    const res = await fetch('/api/cover-letter', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({job_id: jobId})
    });
    const data = await res.json();
    document.getElementById('modal-title').textContent = 'Cover Letter \\u2014 ' + data.job_title + ' at ' + data.company;
    document.getElementById('modal-body').textContent = data.letter;
    log('Generated AI cover letter for ' + data.company);
  } catch(e) {
    document.getElementById('modal-body').textContent = 'Error generating cover letter.';
  }
}

async function autoApply(jobId) {
  if (!confirm('Generate cover letter and mark as applied?')) return;
  setStatus('Applying...', true);
  try {
    const res = await fetch('/api/auto-apply', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({job_id: jobId})
    });
    const data = await res.json();
    if (res.ok) {
      setStatus(data.message, false);
      log(data.message);
      await refreshJobs();
      await loadStats();
      document.getElementById('modal').classList.add('active');
      document.getElementById('modal-title').textContent = 'Applied \\u2014 ' + data.job_title + ' at ' + data.company;
      document.getElementById('modal-body').textContent = data.letter;
    } else {
      setStatus(data.error, false);
    }
  } catch(e) {
    setStatus('Apply failed: ' + e.message, false);
  }
}

function closeModal() {
  document.getElementById('modal').classList.remove('active');
}

function copyLetter() {
  const text = document.getElementById('modal-body').textContent;
  navigator.clipboard.writeText(text);
  log('Cover letter copied to clipboard');
}

async function checkEmails() {
  setStatus('Checking emails...', true);
  log('Checking email responses');
  try {
    const res = await fetch('/api/email-check');
    const data = await res.json();
    document.getElementById('stat-emails').textContent = data.count;
    const el = document.getElementById('email-list');
    if (!data.emails.length) {
      el.innerHTML = '<div class="empty">No matching email responses found.</div>';
      setStatus('No new email responses', false);
      log('No email responses found');
      return;
    }
    el.innerHTML = data.emails.map(e => '<div class="email-item"><div class="email-subject">' + escapeHtml(e.subject) + '</div><div class="email-meta">' + escapeHtml(e.sender) + ' &middot; ' + escapeHtml(e.date) + '</div></div>').join('');
    setStatus(data.count + ' email responses found', false);
    log(data.count + ' email responses found');
  } catch(e) {
    setStatus('Email check failed', false);
    log('Email check failed: ' + e.message);
  }
}

async function uploadResume(input) {
  const file = input.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append('file', file);
  setStatus('Uploading resume...', true);
  try {
    const res = await fetch('/api/upload-resume', {method:'POST', body: formData});
    const data = await res.json();
    if (res.ok) {
      setStatus('Resume uploaded!', false);
      log('Resume uploaded: ' + file.name);
      await loadResumeStatus();
      loadChecklist();
      regenerateLetter();
    } else {
      setStatus(data.error, false);
    }
  } catch(e) {
    setStatus('Upload failed', false);
  }
  input.value = '';
}

let resumeUploaded = false;
async function loadResumeStatus() {
  try {
    const res = await fetch('/api/resume-status');
    const data = await res.json();
    resumeUploaded = data.uploaded;
    const el = document.getElementById('resume-status');
    const el2 = document.getElementById('resume-status-panel');
    const warn = document.getElementById('cover-letter-resume-warning');
    if (data.uploaded) {
      el.innerHTML = '<span class="resume-badge uploaded">&#10003; Resume uploaded</span>';
      if (el2) el2.textContent = 'resume.pdf uploaded';
      if (warn) warn.style.display = 'none';
    } else {
      el.innerHTML = '<span class="resume-badge missing">No resume</span>';
      if (el2) el2.textContent = 'No resume uploaded';
      if (warn) warn.style.display = '';
    }
  } catch(e) {}
}

function openSettings() {
  document.getElementById('settings-overlay').classList.add('active');
  document.getElementById('settings-panel').classList.add('active');
  loadProfile();
}

function closeSettings() {
  document.getElementById('settings-overlay').classList.remove('active');
  document.getElementById('settings-panel').classList.remove('active');
}

async function loadProfile() {
  try {
    const res = await fetch('/api/profile');
    const data = await res.json();
    document.getElementById('set-name').value = data.name || '';
    document.getElementById('set-email').value = data.email || '';
    document.getElementById('set-location').value = data.location || 'remote';
    document.getElementById('set-jobtype').value = data.job_type || 'full-time';
    currentTags = data.keywords || [];
    currentPlatforms = data.platforms || ['remoteok'];
    document.getElementById('set-writing-style').value = data.writing_style || '';
    renderTags();
    renderPlatforms();
    updatePlatformToggles();
    syncFilterBar(data);
  } catch(e) {}
}

async function saveSettings() {
  currentPlatforms = [];
  Object.keys(ALL_PLATFORMS).forEach(key => {
    const cb = document.getElementById('plt-' + key);
    if (cb && cb.checked) currentPlatforms.push(key);
  });
  if (!currentPlatforms.length) currentPlatforms = ['remoteok'];

  const profile = {
    name: document.getElementById('set-name').value,
    email: document.getElementById('set-email').value,
    keywords: currentTags,
    location: document.getElementById('set-location').value,
    job_type: document.getElementById('set-jobtype').value,
    platforms: currentPlatforms,
    writing_style: document.getElementById('set-writing-style').value
  };
  try {
    const res = await fetch('/api/profile', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(profile)
    });
    const data = await res.json();
    log('Settings saved');
    setStatus('Settings saved!', false);
    closeSettings();
    renderPlatforms();
    loadChecklist();
  } catch(e) {
    setStatus('Failed to save settings', false);
  }
}

function renderTags() {
  const container = document.getElementById('tags-container');
  const input = document.getElementById('tag-input');
  container.querySelectorAll('.tag').forEach(t => t.remove());
  currentTags.forEach((tag, i) => {
    const el = document.createElement('span');
    el.className = 'tag';
    el.innerHTML = escapeHtml(tag) + '<span class="tag-remove" onclick="removeTag(' + i + ')">&times;</span>';
    container.insertBefore(el, input);
  });
}

function removeTag(index) {
  currentTags.splice(index, 1);
  renderTags();
}

document.getElementById('tag-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && this.value.trim()) {
    e.preventDefault();
    const val = this.value.trim().toLowerCase();
    if (!currentTags.includes(val)) {
      currentTags.push(val);
      renderTags();
    }
    this.value = '';
  }
  if (e.key === 'Backspace' && !this.value && currentTags.length) {
    currentTags.pop();
    renderTags();
  }
});

async function loadChecklist() {
  try {
    const res = await fetch('/api/checklist');
    const data = await res.json();
    const el = document.getElementById('setup-checklist');
    if (data.complete) {
      el.style.display = 'none';
      return;
    }
    el.style.display = 'block';
    toggleCheckItem('cl-resume', data.resume);
    toggleCheckItem('cl-keywords', data.keywords);
    toggleCheckItem('cl-platform', data.platform);
    toggleCheckItem('cl-search', data.search);
    const done = [data.resume, data.keywords, data.platform, data.search].filter(Boolean).length;
    document.getElementById('cl-progress').style.width = (done / 4 * 100) + '%';
  } catch(e) {}
}

function toggleCheckItem(id, done) {
  const el = document.getElementById(id);
  if (done) el.classList.add('done');
  else el.classList.remove('done');
}

function startTour() {
  introJs().setOptions({
    showProgress: true,
    showBullets: true,
    exitOnOverlayClick: true,
    doneLabel: 'Get Started!',
    nextLabel: 'Next &rarr;',
    prevLabel: '&larr; Back'
  }).oncomplete(function() {
    localStorage.setItem('jobflow_tour_done', '1');
  }).onexit(function() {
    localStorage.setItem('jobflow_tour_done', '1');
  }).start();
}

// Filter Bar - Tags
function renderFilterTags() {
  const container = document.getElementById('filter-tags');
  const input = document.getElementById('filter-tag-input');
  container.querySelectorAll('.tag').forEach(t => t.remove());
  filterTags.forEach((tag, i) => {
    const el = document.createElement('span');
    el.className = 'tag';
    el.innerHTML = escapeHtml(tag) + '<span class="tag-remove" onclick="removeFilterTag(' + i + ')">&times;</span>';
    container.insertBefore(el, input);
  });
}
function removeFilterTag(i) { filterTags.splice(i, 1); renderFilterTags(); }
document.getElementById('filter-tag-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && this.value.trim()) {
    e.preventDefault();
    const val = this.value.trim().toLowerCase();
    if (!filterTags.includes(val)) { filterTags.push(val); renderFilterTags(); }
    this.value = '';
  }
  if (e.key === 'Backspace' && !this.value && filterTags.length) { filterTags.pop(); renderFilterTags(); }
});

// Filter Bar - only show connected platforms
function renderFilterPlatforms() {
  const el = document.getElementById('filter-platforms');
  const connectedKeys = Object.entries(platformConnected).filter(([k,v]) => v).map(([k]) => k);
  if (!connectedKeys.length) {
    el.innerHTML = '<span style="font-size:12px;color:var(--text2)">No platforms connected</span>';
    return;
  }
  el.innerHTML = connectedKeys.map(key => {
    const name = ALL_PLATFORMS[key] || key;
    const active = currentPlatforms.includes(key);
    return '<div class="filter-plt ' + (active ? 'active' : '') + '" onclick="toggleFilterPlatform(&#39;' + key + '&#39;)">' +
      '<span class="plt-status" style="color:var(--green)">&#9679;</span>' + name + '</div>';
  }).join('');
}
function toggleFilterPlatform(key) {
  if (currentPlatforms.includes(key)) {
    if (currentPlatforms.length > 1) currentPlatforms = currentPlatforms.filter(p => p !== key);
  } else {
    currentPlatforms.push(key);
  }
  renderFilterPlatforms();
  renderPlatforms();
  updatePlatformToggles();
}

// Find Jobs with filter bar values
async function findJobsWithFilters() {
  // Save filter bar values to profile first
  const profile = {
    name: '',
    email: '',
    keywords: filterTags,
    location: document.getElementById('filter-location').value,
    job_type: document.getElementById('filter-jobtype').value,
    platforms: currentPlatforms
  };
  // Try to preserve name/email from existing profile
  try {
    const res = await fetch('/api/profile');
    const existing = await res.json();
    profile.name = existing.name || '';
    profile.email = existing.email || '';
  } catch(e) {}
  await fetch('/api/profile', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(profile)});
  // Now search
  await findJobs();
}

// Campaign state
let campaignRunning = false;
let campaignPlatformStats = {};
let campaignAppliedJobs = {};
let campaignSelectedPlatform = null;
const LIMIT_PER_PLATFORM = 50;

// Open campaign modal
async function openApplyPanel() {
  document.getElementById('campaign-overlay').classList.add('active');
  document.getElementById('campaign-dot').classList.add('pulsing');
  document.getElementById('campaign-log').innerHTML = '';
  campaignAppliedJobs = {};
  campaignSelectedPlatform = null;
  showCampaignView('log');

  let limits = {};
  try { const r = await fetch('/api/daily-limits'); limits = await r.json(); } catch(e) {}

  campaignPlatformStats = {};
  currentPlatforms.forEach(p => {
    const lim = limits[p] || {applied: 0, limit: LIMIT_PER_PLATFORM};
    campaignPlatformStats[p] = {
      applied: 0, total: 0,
      already_today: lim.applied,
      status: lim.applied >= lim.limit ? 'limit_reached' : 'waiting'
    };
    campaignAppliedJobs[p] = [];
  });
  renderCampaignCards();

  if (currentPlatforms.every(p => campaignPlatformStats[p].status === 'limit_reached')) {
    document.getElementById('campaign-dot').classList.remove('pulsing');
    document.getElementById('campaign-footer-info').textContent = 'Limit reached — resets tomorrow';
    addCampaignLog('_all', 'done', 'Daily limit reached on all platforms. Resets in 24 hours.');
    campaignRunning = false;
    return;
  }

  campaignRunning = true;
  startCampaignStream();
}

function closeCampaign() {
  document.getElementById('campaign-overlay').classList.remove('active');
  document.getElementById('campaign-dot').classList.remove('pulsing');
  refreshJobs();
  loadStats();
}

function renderCampaignCards() {
  const el = document.getElementById('campaign-cards');
  el.innerHTML = currentPlatforms.map(pk => {
    const s = campaignPlatformStats[pk] || {};
    const name = ALL_PLATFORMS[pk] || pk;
    const applied = s.applied || 0;
    const total = s.total || 0;
    const already = s.already_today || 0;

    // Progress bar percentage (out of 50 limit)
    const barApplied = already + applied;
    const pct = Math.min(100, barApplied / LIMIT_PER_PLATFORM * 100);

    let statusClass = s.status || 'waiting';
    if (statusClass === 'limit_reached') statusClass = 'limit';
    const barClass = statusClass;

    const statusMap = {
      waiting:       ['&#9203;', 'Waiting'],
      running:       ['&#128994;', 'Running...'],
      done:          ['&#10004;', 'Done'],
      failed:        ['&#10008;', 'Failed'],
      limit_reached: ['&#128293;', 'Limit reached']
    };
    const [icon, text] = statusMap[s.status] || statusMap.waiting;
    const selected = campaignSelectedPlatform === pk ? ' selected' : '';
    const countText = barApplied + '/' + LIMIT_PER_PLATFORM;

    return '<div class="campaign-card' + selected + '" onclick="selectCampaignPlatform(&#39;' + pk + '&#39;)">' +
      '<div class="campaign-card-name">' + name + '</div>' +
      '<div class="campaign-bar-wrap"><div class="campaign-bar-fill ' + barClass + '" style="width:' + pct + '%"></div></div>' +
      '<div class="campaign-count">' + countText + '</div>' +
      '<div class="campaign-status ' + statusClass + '">' + icon + ' ' + text + '</div>' +
    '</div>';
  }).join('');
}

function selectCampaignPlatform(pk) {
  if (campaignSelectedPlatform === pk) {
    campaignSelectedPlatform = null;
    showCampaignView('log');
    filterCampaignLog(null);
  } else {
    campaignSelectedPlatform = pk;
    if (!campaignRunning && campaignAppliedJobs[pk]?.length > 0) {
      showCampaignView('history');
      renderCampaignHistory(pk);
    } else {
      showCampaignView('log');
      filterCampaignLog(pk);
    }
  }
  renderCampaignCards();
}

function showCampaignView(view) {
  document.getElementById('campaign-view-log').classList.toggle('active', view === 'log');
  document.getElementById('campaign-view-history').classList.toggle('active', view === 'history');
}

function filterCampaignLog(pk) {
  document.getElementById('campaign-log').querySelectorAll('.campaign-log-line').forEach(line => {
    line.style.display = (!pk || !line.dataset.platform || line.dataset.platform === pk) ? '' : 'none';
  });
}

function addCampaignLog(pk, cls, text) {
  const logEl = document.getElementById('campaign-log');
  const line = document.createElement('div');
  line.className = 'campaign-log-line ' + cls;
  line.textContent = text;
  if (pk && pk !== '_all') line.dataset.platform = pk;
  logEl.appendChild(line);
  logEl.scrollTop = logEl.scrollHeight;
}

async function renderCampaignHistory(pk) {
  const name = ALL_PLATFORMS[pk] || pk;
  let apps = campaignAppliedJobs[pk] || [];
  try {
    const r = await fetch('/api/campaign-history?platform=' + pk);
    const d = await r.json();
    if (d.applications?.length > apps.length) apps = d.applications;
  } catch(e) {}

  document.getElementById('history-header').textContent = name + ' — ' + apps.length + ' applications today';
  document.getElementById('history-list').innerHTML = !apps.length
    ? '<div style="padding:24px;text-align:center;color:var(--text2)">No applications yet</div>'
    : apps.map(a => {
        const t = escapeHtml(a.title||''), c = escapeHtml(a.company||''), l = a.link||'';
        return '<div class="history-item"><div class="history-item-left"><span class="history-item-check">&#10003;</span><div class="history-item-info"><div class="history-item-title">' + t + '</div><div class="history-item-company">' + c + '</div></div></div>' +
          (l ? '<a class="history-item-link" href="' + escapeHtml(l) + '" target="_blank">Open &#8599;</a>' : '') + '</div>';
      }).join('');
}

function startCampaignStream() {
  const es = new EventSource('/api/apply-stream');
  let totalApplied = parseInt(document.getElementById('stat-applied').textContent) || 0;

  es.onmessage = function(event) {
    const data = JSON.parse(event.data);
    const pk = data.platform_key || '';

    if (data.platform_stats) {
      Object.entries(data.platform_stats).forEach(([k, v]) => {
        if (campaignPlatformStats[k]) campaignPlatformStats[k] = v;
      });
      renderCampaignCards();
    }

    switch(data.type) {
      case 'start':
        addCampaignLog('_all', 'platform-header', data.message);
        break;
      case 'platform_start':
        addCampaignLog(pk, 'platform-header', data.message);
        break;
      case 'platform_skip':
        addCampaignLog(pk, 'error', data.message);
        break;
      case 'generating':
        addCampaignLog(pk, 'generating', data.message);
        break;
      case 'applied':
        addCampaignLog(pk, 'applied', data.message);
        if (pk && campaignAppliedJobs[pk]) {
          campaignAppliedJobs[pk].push({title: data.title, company: data.company, link: data.link || ''});
        }
        totalApplied++;
        document.getElementById('stat-applied').textContent = totalApplied;
        document.getElementById('campaign-footer-info').textContent = data.applied + '/' + data.total + ' applied';
        log('Applied: ' + data.title + ' @ ' + data.company);
        break;
      case 'error':
        addCampaignLog(pk, 'error', data.message);
        break;
      case 'platform_done':
        addCampaignLog(pk, 'platform-header', data.message);
        if (campaignSelectedPlatform === pk && campaignAppliedJobs[pk]?.length > 0) {
          showCampaignView('history');
          renderCampaignHistory(pk);
        }
        break;
      case 'done':
        addCampaignLog('_all', 'done', data.message);
        document.getElementById('campaign-dot').classList.remove('pulsing');
        document.getElementById('campaign-footer-info').textContent = data.message;
        campaignRunning = false;
        es.close();
        refreshJobs();
        loadStats();
        renderCampaignCards();
        break;
    }
    if (campaignSelectedPlatform) filterCampaignLog(campaignSelectedPlatform);
  };

  es.onerror = function() {
    es.close();
    addCampaignLog('_all', 'error', 'Connection lost.');
    document.getElementById('campaign-dot').classList.remove('pulsing');
    campaignRunning = false;
  };
}

// Platform Connect Modal — native browser flow
let connectingPlatform = '';
function openConnectModal(platformKey, platformName) {
  connectingPlatform = platformKey;
  document.getElementById('connect-modal-title').textContent = 'Connect ' + platformName;
  document.getElementById('connect-platform-name').textContent = platformName;
  document.getElementById('connect-step1').style.display = '';
  document.getElementById('connect-step2').style.display = 'none';
  document.getElementById('connect-modal').classList.add('active');
}
function closeConnectModal() {
  document.getElementById('connect-modal').classList.remove('active');
}
async function connectStep1() {
  // Open login page in system browser
  await fetch('/api/platform/open-login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({platform:connectingPlatform})});
  // Show step 2
  document.getElementById('connect-step1').style.display = 'none';
  document.getElementById('connect-step2').style.display = '';
}
async function connectConfirm() {
  // User confirms they logged in
  await fetch('/api/platform/confirm-connected', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({platform:connectingPlatform})});
  closeConnectModal();
  log('Connected to ' + (ALL_PLATFORMS[connectingPlatform] || connectingPlatform));
  await loadPlatformStatus();
}
async function disconnectPlatform(platformKey) {
  if (!confirm('Disconnect ' + (ALL_PLATFORMS[platformKey] || platformKey) + '?')) return;
  await fetch('/api/platform/disconnect', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({platform:platformKey})});
  log('Disconnected ' + (ALL_PLATFORMS[platformKey] || platformKey));
  await loadPlatformStatus();
}

// Verify Connection Modal
let verifyingPlatform = '';
async function openVerifyModal(platformKey) {
  verifyingPlatform = platformKey;
  const name = ALL_PLATFORMS[platformKey] || platformKey;
  document.getElementById('verify-modal-title').textContent = 'Verify ' + name;
  document.getElementById('verify-platform-name').textContent = name;
  // Open platform in system browser
  await fetch('/api/platform/open-verify', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({platform:platformKey})});
  document.getElementById('verify-modal').classList.add('active');
}
function closeVerifyModal() {
  document.getElementById('verify-modal').classList.remove('active');
}
async function verifyYes() {
  closeVerifyModal();
  log((ALL_PLATFORMS[verifyingPlatform] || verifyingPlatform) + ' — still connected');
}
async function verifyNo() {
  await fetch('/api/platform/disconnect', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({platform:verifyingPlatform})});
  closeVerifyModal();
  log('Disconnected ' + (ALL_PLATFORMS[verifyingPlatform] || verifyingPlatform));
  await loadPlatformStatus();
}

// Platform Status (sidebar with Free/Paid sections)
async function loadPlatformStatus() {
  try {
    const res = await fetch('/api/platform/status');
    const status = await res.json();
    platformConnected = status;
    renderFilterPlatforms();
    const el = document.getElementById('platform-list-sidebar');

    function renderPlatformRow(key, name, connected, isPaid) {
      const dot = connected ? 'active' : 'inactive';
      let actions = '';
      if (isPaid) {
        if (connected) {
          actions = '<div style="display:flex;gap:6px">' +
            '<button class="btn btn-sm btn-secondary" style="font-size:11px;padding:4px 8px" onclick="openVerifyModal(&#39;' + key + '&#39;)">Verify &#8635;</button>' +
            '<button class="btn btn-sm" style="background:#e74c3c;color:#fff;border:none;font-size:11px;padding:4px 8px;border-radius:6px;cursor:pointer" onclick="disconnectPlatform(&#39;' + key + '&#39;)">&#10005;</button>' +
            '</div>';
        } else {
          actions = '<a class="btn btn-sm" style="background:var(--yellow);color:#000;border:none;font-size:11px;padding:4px 10px;border-radius:6px;text-decoration:none;cursor:pointer" href="' + (PAID_URLS[key]||'#') + '" target="_blank" title="This platform requires a paid subscription">Upgrade &#8599;</a>';
        }
      } else {
        if (connected) {
          actions = '<div style="display:flex;gap:6px">' +
            '<button class="btn btn-sm btn-secondary" style="font-size:11px;padding:4px 8px" onclick="openVerifyModal(&#39;' + key + '&#39;)">Verify &#8635;</button>' +
            '<button class="btn btn-sm" style="background:#e74c3c;color:#fff;border:none;font-size:11px;padding:4px 8px;border-radius:6px;cursor:pointer" onclick="disconnectPlatform(&#39;' + key + '&#39;)">&#10005;</button>' +
            '</div>';
        } else {
          actions = '<button class="btn btn-sm btn-secondary" onclick="openConnectModal(&#39;' + key + '&#39;,&#39;' + name + '&#39;)">Connect</button>';
        }
      }
      return '<div class="platform"><div class="platform-info"><div class="platform-dot ' + dot + '"></div><span class="platform-name">' + name + '</span></div>' + actions + '</div>';
    }

    let html = '<div style="font-size:12px;color:var(--green);margin-bottom:8px;font-weight:600">Free Platforms</div>';
    FREE_PLATFORMS.forEach(key => {
      html += renderPlatformRow(key, ALL_PLATFORMS[key], status[key], false);
    });
    html += '<div style="font-size:12px;color:var(--yellow);margin:14px 0 8px;font-weight:600">Premium Platforms</div>';
    PAID_PLATFORMS.forEach(key => {
      html += renderPlatformRow(key, ALL_PLATFORMS[key], status[key], true);
    });
    el.innerHTML = html;

    // Check daily limits for campaign button
    let limits = {};
    try {
      const limRes = await fetch('/api/daily-limits');
      limits = await limRes.json();
    } catch(e2) {}
    const connectedPlatforms = Object.entries(status).filter(([k,v]) => v);
    const anyConnected = connectedPlatforms.length > 0;
    const allAtLimit = currentPlatforms.every(p => (limits[p]?.applied || 0) >= LIMIT_PER_PLATFORM);
    const applyBtn = document.getElementById('btn-apply-all');
    if (applyBtn) {
      if (!anyConnected) {
        applyBtn.disabled = true;
        applyBtn.title = 'Connect at least one platform first';
        applyBtn.textContent = 'Start Campaign';
      } else if (allAtLimit) {
        applyBtn.disabled = true;
        applyBtn.title = 'Daily limit reached on all platforms';
        applyBtn.textContent = 'Limit reached — resets tomorrow';
      } else {
        applyBtn.disabled = false;
        applyBtn.title = '';
        applyBtn.textContent = 'Start Campaign';
      }
    }
  } catch(e) {}
}

// Cover Letter Preview
async function regenerateLetter() {
  const btn = document.getElementById('regen-btn');
  btn.disabled = true; btn.textContent = 'Generating...';
  const keywords = filterTags.join(', ') || currentTags.join(', ');
  try {
    const res = await fetch('/api/cover-letter-preview', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({keywords})});
    const data = await res.json();
    document.getElementById('cover-letter-preview').value = data.letter;
  } catch(e) { console.error(e); }
  finally { btn.disabled = false; btn.textContent = 'Regenerate'; }
}

async function saveLetterTemplate() {
  const letter = document.getElementById('cover-letter-preview').value;
  if (!letter.trim()) return;
  await fetch('/api/cover-letter-template', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({template:letter})});
  log('Cover letter template saved');
  const btn = document.getElementById('save-letter-btn');
  btn.textContent = 'Saved!';
  setTimeout(() => btn.textContent = 'Save as Template', 2000);
}

// Sync filter bar with profile
function syncFilterBar(profile) {
  filterTags = profile.keywords || [];
  renderFilterTags();
  document.getElementById('filter-location').value = profile.location || 'remote';
  document.getElementById('filter-jobtype').value = profile.job_type || 'full-time';
  currentPlatforms = profile.platforms || ['remoteok'];
  renderFilterPlatforms();
}

// Init
loadStats();
refreshJobs();
loadResumeStatus().then(() => { if (resumeUploaded) regenerateLetter(); });
loadChecklist();
loadPlatformStatus().then(() => loadProfile());

if (!localStorage.getItem('jobflow_tour_done')) {
  setTimeout(startTour, 500);
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
