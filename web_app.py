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

DAILY_APPLY_LIMIT = 50

from database.db import init_db, save_job, get_all_jobs, get_connection, update_job_status, save_application
from modules.platforms import get_enabled_platforms
from modules.filters import filter_jobs
from modules.telegram_bot import send_notification
from modules.email_parser import check_email_responses
from modules.ai_cover_letter import generate_cover_letter

app = FastAPI(title="JobFlow")
init_db()

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
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


def load_profile():
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, "r") as f:
            return json.load(f)
    return {"name": "", "email": "", "keywords": ["marketing", "content", "automation"], "location": "remote", "job_type": "full-time", "platforms": ["remoteok"]}


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
    """SSE endpoint - streams apply events in real time."""
    async def event_generator():
        jobs = get_all_jobs()
        new_jobs = [j for j in jobs if j["status"] == "new"]

        if not new_jobs:
            yield f"data: {json_lib.dumps({'type':'done','message':'No new jobs to apply to'})}\n\n"
            return

        to_apply = new_jobs[:DAILY_APPLY_LIMIT]
        total = len(to_apply)

        yield f"data: {json_lib.dumps({'type':'start','total':total,'message':f'Starting application process for {total} jobs...'})}\n\n"
        await asyncio.sleep(0.3)

        platforms_order = {}
        for job in to_apply:
            p = job["platform"] if "platform" in job.keys() else "remoteok"
            if p not in platforms_order:
                platforms_order[p] = []
            platforms_order[p].append(job)

        applied_count = 0

        for platform_key, platform_jobs in platforms_order.items():
            platform_names = {"remoteok": "RemoteOK", "indeed": "Indeed", "wellfound": "Wellfound"}
            pname = platform_names.get(platform_key, platform_key.upper())

            yield f"data: {json_lib.dumps({'type':'platform_start','platform':pname,'count':len(platform_jobs),'message':f'--- Starting {pname} ({len(platform_jobs)} jobs) ---'})}\n\n"
            await asyncio.sleep(0.5)

            for i, job in enumerate(platform_jobs):
                title = job["title"]
                company = job["company"]

                yield f"data: {json_lib.dumps({'type':'generating','platform':pname,'job_id':job['id'],'message':f'[{pname}] Generating cover letter for {title} @ {company}...'})}\n\n"
                await asyncio.sleep(0.2)

                try:
                    letter = generate_cover_letter({
                        "title": title,
                        "company": company,
                        "description": job["description"] if "description" in job.keys() else "",
                    })
                    update_job_status(job["id"], "applied")
                    save_application(job["id"], letter)
                    applied_count += 1

                    yield f"data: {json_lib.dumps({'type':'applied','platform':pname,'job_id':job['id'],'title':title,'company':company,'applied':applied_count,'total':total,'message':f'[{pname}] Applied: {title} @ {company}'})}\n\n"
                    await asyncio.sleep(0.8)

                except Exception as e:
                    yield f"data: {json_lib.dumps({'type':'error','platform':pname,'job_id':job['id'],'message':f'[{pname}] Failed: {title} @ {company} - {str(e)}'})}\n\n"
                    await asyncio.sleep(0.3)

            yield f"data: {json_lib.dumps({'type':'platform_done','platform':pname,'message':f'--- {pname} done: {len(platform_jobs)} jobs processed ---'})}\n\n"
            await asyncio.sleep(0.5)

        try:
            send_notification(f"JobFlow: Applied to {applied_count} jobs today!")
        except Exception:
            pass

        yield f"data: {json_lib.dumps({'type':'done','applied':applied_count,'total':total,'message':f'Done! Applied to {applied_count}/{total} jobs today.'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


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
    data = profile.dict()
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

.action-bar{display:flex;gap:12px;align-items:center;margin-bottom:24px;flex-wrap:wrap}
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
.filter-row{display:flex;gap:16px;align-items:flex-end;flex-wrap:wrap}
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
.filter-plt{display:flex;align-items:center;gap:4px;padding:6px 12px;background:var(--surface2);border:1px solid var(--border);border-radius:8px;font-size:12px;cursor:pointer;transition:all .2s;user-select:none}
.filter-plt:hover{border-color:var(--accent)}
.filter-plt.active{background:rgba(108,92,231,.2);border-color:var(--accent);color:var(--accent2)}
.filter-plt input{display:none}
.filter-actions{display:flex;gap:10px;align-items:flex-end}

/* Application Process Panel */
.apply-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.4);z-index:300}
.apply-overlay.active{display:block}
.apply-panel{position:fixed;top:0;right:-520px;width:520px;height:100%;background:#0d1117;border-left:1px solid var(--border);z-index:301;transition:right .3s ease;display:flex;flex-direction:column}
.apply-panel.active{right:0}
.apply-header{padding:20px 24px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:var(--surface)}
.apply-header-left{display:flex;align-items:center;gap:12px}
.apply-dot{width:10px;height:10px;border-radius:50%;background:var(--green)}
.apply-dot.pulsing{animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.apply-title{font-size:18px;font-weight:700}
.apply-progress{padding:16px 24px;border-bottom:1px solid var(--border);background:var(--surface)}
.apply-progress-bar{height:6px;background:var(--surface2);border-radius:3px;overflow:hidden;margin-top:8px}
.apply-progress-fill{height:100%;background:linear-gradient(90deg,var(--green),var(--accent2));border-radius:3px;transition:width .4s;width:0%}
.apply-progress-text{display:flex;justify-content:space-between;font-size:12px;color:var(--text2)}
.apply-platforms{display:flex;gap:8px;padding:12px 24px;border-bottom:1px solid var(--border);background:var(--surface)}
.apply-plt-tab{padding:6px 14px;border-radius:8px;font-size:12px;font-weight:600;background:var(--surface2);color:var(--text2);border:1px solid var(--border)}
.apply-plt-tab.active{background:rgba(108,92,231,.2);color:var(--accent2);border-color:var(--accent)}
.apply-plt-tab.done{background:rgba(16,185,129,.15);color:var(--green);border-color:var(--green)}
.apply-log{flex:1;overflow-y:auto;padding:16px 24px;font-family:'SF Mono',SFMono-Regular,Consolas,'Liberation Mono',Menlo,monospace;font-size:13px;line-height:1.8}
.apply-log-line{padding:2px 0}
.apply-log-line.platform-header{color:var(--blue);font-weight:600;margin-top:8px}
.apply-log-line.generating{color:var(--text2)}
.apply-log-line.applied{color:var(--green)}
.apply-log-line.error{color:var(--red)}
.apply-log-line.done{color:var(--yellow);font-weight:600;margin-top:8px}
.apply-footer{padding:16px 24px;border-top:1px solid var(--border);background:var(--surface);text-align:right}
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
        <button class="btn btn-green" id="btn-apply-all" onclick="openApplyPanel()">Start Application Process</button>
      </div>
    </div>
  </div>

  <div class="action-bar">
    <button class="btn btn-secondary" onclick="refreshJobs()">Refresh Table</button>
    <button class="btn btn-secondary" onclick="checkEmails()">Check Emails</button>
    <div class="resume-section">
      <label class="btn btn-secondary btn-sm" for="resume-input" id="resume-upload-btn">Upload Resume</label>
      <input type="file" id="resume-input" accept=".pdf" onchange="uploadResume(this)">
      <span id="resume-status"></span>
    </div>
    <span id="action-status" style="color:var(--text2);font-size:13px;margin-left:8px"></span>
  </div>

  <div class="grid">
    <div>
      <div class="card">
        <div class="card-title"><span class="dot" style="background:var(--green)"></span>Job Listings</div>
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

  <div class="form-section">Platforms</div>
  <div class="form-group">
    <div class="platform-toggle">
      <span class="platform-toggle-name">RemoteOK</span>
      <label class="toggle"><input type="checkbox" id="plt-remoteok" value="remoteok" checked><span class="toggle-slider"></span></label>
    </div>
    <div class="platform-toggle">
      <span class="platform-toggle-name">Indeed</span>
      <label class="toggle"><input type="checkbox" id="plt-indeed" value="indeed"><span class="toggle-slider"></span></label>
    </div>
    <div class="platform-toggle">
      <span class="platform-toggle-name">Wellfound</span>
      <label class="toggle"><input type="checkbox" id="plt-wellfound" value="wellfound"><span class="toggle-slider"></span></label>
    </div>
  </div>

  <div class="form-section">Resume</div>
  <div class="form-group">
    <label class="btn btn-secondary" for="resume-input-panel" style="display:inline-block;cursor:pointer">Choose PDF File</label>
    <input type="file" id="resume-input-panel" accept=".pdf" onchange="uploadResume(this)" style="display:none">
    <span id="resume-status-panel" style="margin-left:10px;font-size:13px;color:var(--text2)"></span>
  </div>

  <button class="btn btn-green" onclick="saveSettings()" style="width:100%;margin-top:12px">Save Settings</button>
</div>

<!-- Application Process Panel -->
<div class="apply-overlay" id="apply-overlay" onclick="closeApplyPanel()"></div>
<div class="apply-panel" id="apply-panel">
  <div class="apply-header">
    <div class="apply-header-left">
      <div class="apply-dot" id="apply-dot"></div>
      <span class="apply-title">Application Process</span>
    </div>
    <button class="modal-close" onclick="closeApplyPanel()">&times;</button>
  </div>
  <div class="apply-progress">
    <div class="apply-progress-text">
      <span id="apply-count">0 / 0 applied</span>
      <span id="apply-limit">Daily limit: 50</span>
    </div>
    <div class="apply-progress-bar"><div class="apply-progress-fill" id="apply-progress-fill"></div></div>
  </div>
  <div class="apply-platforms" id="apply-platforms"></div>
  <div class="apply-log" id="apply-log"></div>
  <div class="apply-footer">
    <button class="btn btn-secondary btn-sm" onclick="closeApplyPanel()">Close</button>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/intro.js@7.2.0/intro.min.js"></script>
<script>
const statusEl = document.getElementById('action-status');
const logEl = document.getElementById('activity-log');
let currentTags = [];
let filterTags = [];
let currentPlatforms = ['remoteok'];
const ALL_PLATFORMS = {remoteok:'RemoteOK', indeed:'Indeed', wellfound:'Wellfound'};

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

// Platforms display
function renderPlatforms() {
  const el = document.getElementById('platform-list');
  el.innerHTML = Object.entries(ALL_PLATFORMS).map(([key, name]) => {
    const active = currentPlatforms.includes(key);
    return '<div class="platform" onclick="togglePlatform(\\'' + key + '\\')">' +
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
  Object.keys(ALL_PLATFORMS).forEach(key => {
    const cb = document.getElementById('plt-' + key);
    if (cb) cb.checked = currentPlatforms.includes(key);
  });
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

async function refreshJobs() {
  try {
    const res = await fetch('/api/jobs');
    const jobs = await res.json();
    const tbody = document.getElementById('jobs-table');
    if (!jobs.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">No jobs yet. Click "Find Jobs" to start.</td></tr>';
      return;
    }
    tbody.innerHTML = jobs.map(j => {
      const pName = ALL_PLATFORMS[j.platform] || j.platform;
      const actions = j.status === 'applied'
        ? '<button class="btn btn-secondary btn-sm" onclick="genCoverLetter(' + j.id + ')">Letter</button>'
        : '<div class="btn-actions"><button class="btn btn-secondary btn-sm" onclick="genCoverLetter(' + j.id + ')">Letter</button><button class="btn btn-blue btn-sm" onclick="autoApply(' + j.id + ')">Apply</button></div>';
      return '<tr>' +
        '<td><a href="' + escapeHtml(j.link) + '" target="_blank">' + escapeHtml(j.title) + '</a></td>' +
        '<td>' + escapeHtml(j.company) + '</td>' +
        '<td><span class="platform-tag">' + escapeHtml(pName) + '</span></td>' +
        '<td>' + j.date_found.slice(0,10) + '</td>' +
        '<td><span class="status status-' + j.status + '">' + j.status + '</span></td>' +
        '<td>' + actions + '</td>' +
      '</tr>';
    }).join('');
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
      loadResumeStatus();
      loadChecklist();
    } else {
      setStatus(data.error, false);
    }
  } catch(e) {
    setStatus('Upload failed', false);
  }
  input.value = '';
}

async function loadResumeStatus() {
  try {
    const res = await fetch('/api/resume-status');
    const data = await res.json();
    const el = document.getElementById('resume-status');
    const el2 = document.getElementById('resume-status-panel');
    if (data.uploaded) {
      el.innerHTML = '<span class="resume-badge uploaded">&#10003; Resume uploaded</span>';
      if (el2) el2.textContent = 'resume.pdf uploaded';
    } else {
      el.innerHTML = '<span class="resume-badge missing">No resume</span>';
      if (el2) el2.textContent = 'No resume uploaded';
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
    platforms: currentPlatforms
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

// Filter Bar - Platforms
function renderFilterPlatforms() {
  const el = document.getElementById('filter-platforms');
  el.innerHTML = Object.entries(ALL_PLATFORMS).map(([key, name]) => {
    const active = currentPlatforms.includes(key);
    return '<div class="filter-plt ' + (active ? 'active' : '') + '" onclick="toggleFilterPlatform(\\'' + key + '\\')">' + name + '</div>';
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

// Application Process Panel
function openApplyPanel() {
  document.getElementById('apply-overlay').classList.add('active');
  document.getElementById('apply-panel').classList.add('active');
  document.getElementById('apply-dot').classList.add('pulsing');
  document.getElementById('apply-log').innerHTML = '';
  document.getElementById('apply-count').textContent = '0 / 0 applied';
  document.getElementById('apply-progress-fill').style.width = '0%';
  // Show platform tabs
  document.getElementById('apply-platforms').innerHTML = currentPlatforms.map(p =>
    '<div class="apply-plt-tab" id="apply-tab-' + p + '">' + ALL_PLATFORMS[p] + '</div>'
  ).join('');
  startApplyStream();
}
function closeApplyPanel() {
  document.getElementById('apply-overlay').classList.remove('active');
  document.getElementById('apply-panel').classList.remove('active');
  document.getElementById('apply-dot').classList.remove('pulsing');
  refreshJobs();
  loadStats();
}

function startApplyStream() {
  const logEl = document.getElementById('apply-log');
  const es = new EventSource('/api/apply-stream');
  let currentPlatformTab = null;

  es.onmessage = function(event) {
    const data = JSON.parse(event.data);
    const line = document.createElement('div');
    line.className = 'apply-log-line';

    switch(data.type) {
      case 'start':
        line.className += ' platform-header';
        line.textContent = data.message;
        document.getElementById('apply-count').textContent = '0 / ' + data.total + ' applied';
        break;
      case 'platform_start':
        line.className += ' platform-header';
        line.textContent = data.message;
        if (currentPlatformTab) {
          const prevTab = document.getElementById('apply-tab-' + currentPlatformTab);
          if (prevTab) prevTab.className = 'apply-plt-tab done';
        }
        currentPlatformTab = Object.entries(ALL_PLATFORMS).find(([k,v]) => v === data.platform)?.[0];
        if (currentPlatformTab) {
          const tab = document.getElementById('apply-tab-' + currentPlatformTab);
          if (tab) tab.className = 'apply-plt-tab active';
        }
        break;
      case 'generating':
        line.className += ' generating';
        line.textContent = data.message;
        break;
      case 'applied':
        line.className += ' applied';
        line.textContent = data.message;
        document.getElementById('apply-count').textContent = data.applied + ' / ' + data.total + ' applied';
        document.getElementById('apply-progress-fill').style.width = (data.applied / data.total * 100) + '%';
        log('Applied: ' + data.title + ' @ ' + data.company);
        break;
      case 'error':
        line.className += ' error';
        line.textContent = data.message;
        break;
      case 'platform_done':
        line.className += ' platform-header';
        line.textContent = data.message;
        if (currentPlatformTab) {
          const tab = document.getElementById('apply-tab-' + currentPlatformTab);
          if (tab) tab.className = 'apply-plt-tab done';
        }
        break;
      case 'done':
        line.className += ' done';
        line.textContent = data.message;
        document.getElementById('apply-dot').classList.remove('pulsing');
        es.close();
        refreshJobs();
        loadStats();
        break;
    }
    logEl.appendChild(line);
    logEl.scrollTop = logEl.scrollHeight;
  };

  es.onerror = function() {
    es.close();
    const line = document.createElement('div');
    line.className = 'apply-log-line error';
    line.textContent = 'Connection lost.';
    logEl.appendChild(line);
    document.getElementById('apply-dot').classList.remove('pulsing');
  };
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
loadResumeStatus();
loadChecklist();
loadProfile().then(() => { renderPlatforms(); renderFilterPlatforms(); syncFilterBar({keywords:currentTags, location:'remote', job_type:'full-time', platforms:currentPlatforms}); });

if (!localStorage.getItem('jobflow_tour_done')) {
  setTimeout(startTour, 500);
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
