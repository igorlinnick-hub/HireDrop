import sys
import os
import json
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional

from database.db import init_db, save_job, get_all_jobs, get_connection
from modules.scraper import scrape_jobs
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


class ProfileUpdate(BaseModel):
    name: str = ""
    email: str = ""
    keywords: List[str] = []
    location: str = "remote"
    job_type: str = "full-time"


def load_profile():
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, "r") as f:
            return json.load(f)
    return {"name": "", "email": "", "keywords": ["marketing", "content", "automation"], "location": "remote", "job_type": "full-time"}


def save_profile(data):
    with open(PROFILE_PATH, "w") as f:
        json.dump(data, f, indent=2)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


@app.post("/api/find-jobs")
def find_jobs():
    jobs = scrape_jobs()
    if not jobs:
        return {"count": 0, "message": "No jobs found from API"}
    filtered = filter_jobs(jobs)
    if not filtered:
        return {"count": 0, "message": "No new jobs matching keywords"}
    for job in filtered:
        save_job(job["title"], job["company"], job["link"])
    send_notification(f"JobFlow: {len(filtered)} new jobs found!")
    return {"count": len(filtered), "message": f"{len(filtered)} new jobs saved"}


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
    letter = generate_cover_letter({"title": job["title"], "company": job["company"]})
    return {"letter": letter, "job_title": job["title"], "company": job["company"]}


@app.get("/api/email-check")
def email_check():
    responses = check_email_responses()
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
    save_profile(data)
    return {"message": "Profile saved", "profile": data}


@app.get("/api/checklist")
def checklist():
    profile = load_profile()
    has_resume = os.path.exists(RESUME_PATH)
    has_keywords = len(profile.get("keywords", [])) > 0
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM jobs")
    has_searched = cursor.fetchone()[0] > 0
    conn.close()
    return {
        "resume": has_resume,
        "keywords": has_keywords,
        "platform": True,
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

/* Intro.js dark theme overrides */
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

/* Header */
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

/* Layout */
.container{max-width:1400px;margin:0 auto;padding:24px 32px}
.grid{display:grid;grid-template-columns:1fr 340px;gap:24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:24px;margin-bottom:24px}
.card-title{font-size:16px;font-weight:600;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.card-title .dot{width:8px;height:8px;border-radius:50%}

/* Setup Checklist */
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

/* Action Bar */
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

/* Resume Upload */
.resume-section{display:flex;align-items:center;gap:12px}
.resume-badge{display:flex;align-items:center;gap:6px;padding:6px 12px;border-radius:8px;font-size:12px;font-weight:600}
.resume-badge.uploaded{background:rgba(16,185,129,.15);color:var(--green)}
.resume-badge.missing{background:rgba(239,68,68,.15);color:var(--red)}
input[type="file"]{display:none}

/* Table */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:10px 14px;font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--text2);border-bottom:1px solid var(--border)}
td{padding:12px 14px;border-bottom:1px solid var(--border);font-size:14px}
tr:hover td{background:var(--surface2)}
.status{padding:4px 10px;border-radius:6px;font-size:12px;font-weight:600}
.status-new{background:rgba(16,185,129,.15);color:var(--green)}
.status-applied{background:rgba(59,130,246,.15);color:var(--blue)}
.status-interview{background:rgba(245,158,11,.15);color:var(--yellow)}

/* Platforms */
.platform-list{display:flex;flex-direction:column;gap:10px}
.platform{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:var(--surface2);border-radius:10px}
.platform-info{display:flex;align-items:center;gap:10px}
.platform-dot{width:10px;height:10px;border-radius:50%}
.platform-dot.active{background:var(--green)}
.platform-dot.inactive{background:var(--border)}
.platform-name{font-size:14px;font-weight:500}
.platform-badge{font-size:11px;padding:3px 8px;border-radius:6px;font-weight:600}
.badge-connected{background:rgba(16,185,129,.15);color:var(--green)}
.badge-soon{background:rgba(147,149,165,.1);color:var(--text2)}

/* Activity Log */
.log{display:flex;flex-direction:column;gap:8px;max-height:300px;overflow-y:auto}
.log-item{display:flex;align-items:flex-start;gap:10px;padding:8px 12px;background:var(--surface2);border-radius:8px;font-size:13px}
.log-time{color:var(--text2);font-size:11px;white-space:nowrap;min-width:55px}
.log-msg{color:var(--text)}

/* Email */
.email-list{display:flex;flex-direction:column;gap:8px}
.email-item{padding:12px;background:var(--surface2);border-radius:10px}
.email-subject{font-weight:600;font-size:14px;margin-bottom:4px}
.email-meta{font-size:12px;color:var(--text2)}

/* Modal */
.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.6);z-index:100;align-items:center;justify-content:center}
.modal-overlay.active{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:32px;max-width:640px;width:90%;max-height:80vh;overflow-y:auto}
.modal-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
.modal-close{background:none;border:none;color:var(--text2);font-size:24px;cursor:pointer}
.modal-close:hover{color:var(--text)}
.letter-content{white-space:pre-wrap;line-height:1.7;font-size:14px;background:var(--surface2);padding:20px;border-radius:10px}

/* Settings Panel */
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

/* Tags Input */
.tags-container{display:flex;flex-wrap:wrap;gap:8px;padding:10px 14px;background:var(--surface2);border:1px solid var(--border);border-radius:10px;min-height:44px;cursor:text}
.tags-container:focus-within{border-color:var(--accent)}
.tag{display:flex;align-items:center;gap:4px;padding:4px 10px;background:rgba(108,92,231,.2);border:1px solid rgba(108,92,231,.3);border-radius:6px;font-size:13px;color:var(--accent2)}
.tag-remove{cursor:pointer;font-size:16px;line-height:1;opacity:.7}
.tag-remove:hover{opacity:1}
.tag-input{border:none;background:none;color:var(--text);font-size:14px;outline:none;min-width:80px;flex:1}
.tag-input::placeholder{color:var(--text2)}

/* Spinner */
.spinner{display:inline-block;width:16px;height:16px;border:2px solid var(--border);border-top-color:var(--accent2);border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle;margin-right:8px}
@keyframes spin{to{transform:rotate(360deg)}}

/* Empty */
.empty{text-align:center;padding:40px;color:var(--text2)}

/* Scrollbar */
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
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
        <div class="val" id="stat-emails">-</div>
        <div class="lbl">Responses</div>
      </div>
    </div>
    <button class="btn-icon" onclick="openSettings()" title="Settings">&#9881;</button>
  </div>
</div>

<div class="container">
  <!-- Setup Checklist -->
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
      <div class="checklist-item done" id="cl-platform">
        <span class="check-icon">&#10003;</span>
        <span>Connect Platform</span>
      </div>
      <div class="checklist-item" id="cl-search" onclick="findJobs()" data-intro="Click Find Jobs to search connected platforms for matching opportunities." data-step="4" data-title="Find Jobs">
        <span class="check-icon">&#10003;</span>
        <span>Start First Search</span>
      </div>
    </div>
    <div class="checklist-progress"><div class="checklist-progress-bar" id="cl-progress" style="width:25%"></div></div>
  </div>

  <div class="action-bar">
    <button class="btn btn-primary" id="btn-find" onclick="findJobs()">Find Jobs</button>
    <button class="btn btn-secondary" onclick="refreshJobs()">Refresh Table</button>
    <button class="btn btn-secondary" onclick="checkEmails()">Check Emails</button>
    <div class="resume-section" data-intro="Once you find a great match, generate a personalized cover letter with one click." data-step="5" data-title="Generate Cover Letters">
      <label class="btn btn-secondary btn-sm" for="resume-input" id="resume-upload-btn">Upload Resume</label>
      <input type="file" id="resume-input" accept=".pdf" onchange="uploadResume(this)">
      <span id="resume-status"></span>
    </div>
    <span id="action-status" style="color:var(--text2);font-size:13px;margin-left:8px"></span>
  </div>

  <div class="grid">
    <div>
      <!-- Jobs Table -->
      <div class="card">
        <div class="card-title"><span class="dot" style="background:var(--green)"></span>Job Listings</div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th>Title</th><th>Company</th><th>Date</th><th>Status</th><th>Action</th></tr>
            </thead>
            <tbody id="jobs-table"><tr><td colspan="5" class="empty">No jobs yet. Click "Find Jobs" to start.</td></tr></tbody>
          </table>
        </div>
      </div>

      <!-- Email Responses -->
      <div class="card">
        <div class="card-title"><span class="dot" style="background:var(--blue)"></span>Email Responses</div>
        <div class="email-list" id="email-list">
          <div class="empty">No email responses checked yet.</div>
        </div>
      </div>
    </div>

    <div>
      <!-- Platforms -->
      <div class="card">
        <div class="card-title"><span class="dot" style="background:var(--accent)"></span>Platforms</div>
        <div class="platform-list">
          <div class="platform">
            <div class="platform-info"><div class="platform-dot active"></div><span class="platform-name">RemoteOK</span></div>
            <span class="platform-badge badge-connected">Connected</span>
          </div>
          <div class="platform">
            <div class="platform-info"><div class="platform-dot inactive"></div><span class="platform-name">Indeed</span></div>
            <span class="platform-badge badge-soon">Coming Soon</span>
          </div>
          <div class="platform">
            <div class="platform-info"><div class="platform-dot inactive"></div><span class="platform-name">Wellfound</span></div>
            <span class="platform-badge badge-soon">Coming Soon</span>
          </div>
          <div class="platform">
            <div class="platform-info"><div class="platform-dot inactive"></div><span class="platform-name">Greenhouse</span></div>
            <span class="platform-badge badge-soon">Coming Soon</span>
          </div>
          <div class="platform">
            <div class="platform-info"><div class="platform-dot inactive"></div><span class="platform-name">Lever</span></div>
            <span class="platform-badge badge-soon">Coming Soon</span>
          </div>
        </div>
      </div>

      <!-- Activity Log -->
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

  <div class="form-section">Resume</div>
  <div class="form-group">
    <label class="btn btn-secondary" for="resume-input-panel" style="display:inline-block;cursor:pointer">Choose PDF File</label>
    <input type="file" id="resume-input-panel" accept=".pdf" onchange="uploadResume(this)" style="display:none">
    <span id="resume-status-panel" style="margin-left:10px;font-size:13px;color:var(--text2)"></span>
  </div>

  <button class="btn btn-green" onclick="saveSettings()" style="width:100%;margin-top:12px">Save Settings</button>
</div>

<script src="https://cdn.jsdelivr.net/npm/intro.js@7.2.0/intro.min.js"></script>
<script>
const statusEl = document.getElementById('action-status');
const logEl = document.getElementById('activity-log');
let currentTags = [];

function log(msg) {
  const now = new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  const item = document.createElement('div');
  item.className = 'log-item';
  item.innerHTML = '<span class="log-time">' + now + '</span><span class="log-msg">' + msg + '</span>';
  logEl.prepend(item);
}

function setStatus(msg, loading) {
  statusEl.innerHTML = (loading ? '<span class="spinner"></span>' : '') + msg;
}

// Stats
async function loadStats() {
  try {
    const res = await fetch('/api/stats');
    const data = await res.json();
    document.getElementById('stat-total').textContent = data.total_jobs;
    document.getElementById('stat-today').textContent = data.new_today;
  } catch(e) {}
}

// Jobs
async function findJobs() {
  const btn = document.getElementById('btn-find');
  btn.disabled = true;
  setStatus('Searching RemoteOK...', true);
  log('Started job search on RemoteOK');
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
      tbody.innerHTML = '<tr><td colspan="5" class="empty">No jobs yet. Click "Find Jobs" to start.</td></tr>';
      return;
    }
    tbody.innerHTML = jobs.map(j => `
      <tr>
        <td><a href="${j.link}" target="_blank">${j.title}</a></td>
        <td>${j.company}</td>
        <td>${j.date_found.slice(0,10)}</td>
        <td><span class="status status-${j.status}">${j.status}</span></td>
        <td><button class="btn btn-secondary btn-sm" onclick="genCoverLetter(${j.id})">Cover Letter</button></td>
      </tr>
    `).join('');
  } catch(e) {}
}

// Cover Letter
async function genCoverLetter(jobId) {
  document.getElementById('modal').classList.add('active');
  document.getElementById('modal-body').textContent = 'Generating...';
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
    log('Generated cover letter for ' + data.company);
  } catch(e) {
    document.getElementById('modal-body').textContent = 'Error generating cover letter.';
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

// Emails
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
    el.innerHTML = data.emails.map(e => `
      <div class="email-item">
        <div class="email-subject">${e.subject}</div>
        <div class="email-meta">${e.sender} &middot; ${e.date}</div>
      </div>
    `).join('');
    setStatus(data.count + ' email responses found', false);
    log(data.count + ' email responses found');
  } catch(e) {
    setStatus('Email check failed', false);
    log('Email check failed: ' + e.message);
  }
}

// Resume Upload
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

// Settings Panel
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
    renderTags();
  } catch(e) {}
}

async function saveSettings() {
  const profile = {
    name: document.getElementById('set-name').value,
    email: document.getElementById('set-email').value,
    keywords: currentTags,
    location: document.getElementById('set-location').value,
    job_type: document.getElementById('set-jobtype').value
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
    loadChecklist();
  } catch(e) {
    setStatus('Failed to save settings', false);
  }
}

// Tags Input
function renderTags() {
  const container = document.getElementById('tags-container');
  const input = document.getElementById('tag-input');
  container.querySelectorAll('.tag').forEach(t => t.remove());
  currentTags.forEach((tag, i) => {
    const el = document.createElement('span');
    el.className = 'tag';
    el.innerHTML = tag + '<span class="tag-remove" onclick="removeTag(' + i + ')">&times;</span>';
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

// Setup Checklist
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

// Onboarding Tour
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

// Init
loadStats();
refreshJobs();
loadResumeStatus();
loadChecklist();

if (!localStorage.getItem('jobflow_tour_done')) {
  setTimeout(startTour, 500);
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
