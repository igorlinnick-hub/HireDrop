# HireDrop — Technical Documentation

## Table of Contents

1. [Overview](#overview)
2. [Tech Stack](#tech-stack)
3. [Project Structure](#project-structure)
4. [Architecture Diagram](#architecture-diagram)
5. [Database Schema](#database-schema)
6. [Backend (FastAPI)](#backend-fastapi)
   - [API Endpoints](#api-endpoints)
   - [Data Storage](#data-storage)
   - [Pydantic Models](#pydantic-models)
7. [Platform Scrapers](#platform-scrapers)
   - [Architecture](#scraper-architecture)
   - [Implemented Platforms](#implemented-platforms)
   - [Adding a New Platform](#adding-a-new-platform)
8. [Job Filtering](#job-filtering)
9. [AI Cover Letter Generation](#ai-cover-letter-generation)
10. [Chrome Extension](#chrome-extension)
    - [Architecture](#extension-architecture)
    - [Manifest & Permissions](#manifest--permissions)
    - [Background Service Worker](#background-service-worker)
    - [Content Script — 3-Phase Automation](#content-script--3-phase-automation)
    - [Popup UI](#popup-ui)
    - [Message Protocol](#message-protocol)
    - [Anti-Detection Techniques](#anti-detection-techniques)
11. [Telegram Notifications](#telegram-notifications)
12. [Email Monitoring](#email-monitoring)
13. [Encryption Module](#encryption-module)
14. [Frontend (Dashboard & Onboarding)](#frontend-dashboard--onboarding)
15. [CLI Interface](#cli-interface)
16. [Deployment](#deployment)
17. [Environment Variables](#environment-variables)
18. [Dependencies](#dependencies)
19. [Known Limitations & TODOs](#known-limitations--todos)

---

## Overview

**HireDrop** is an AI-powered job search automation platform. It scrapes job listings from multiple platforms, generates personalized cover letters using Claude AI, and automatically applies to jobs on Indeed.com via a Chrome Extension.

**Core value proposition:** User sets up profile once → system finds jobs → AI writes cover letters → Chrome extension auto-applies → Telegram notifies about responses.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | Python 3, FastAPI, Uvicorn |
| **Database** | SQLite (file-based, `database/hiredrop.db`) |
| **AI** | Anthropic Claude API (claude-sonnet-4-20250514) |
| **Web Scraping** | BeautifulSoup4, Requests |
| **PDF Parsing** | pdfplumber |
| **Frontend** | Vanilla HTML/CSS/JS (embedded in `web_app.py` as string templates) |
| **Browser Extension** | Chrome Manifest V3 (JS) |
| **Notifications** | Telegram Bot API |
| **Email** | IMAP4 (Gmail default) |
| **Encryption** | cryptography (Fernet/AES-128) |
| **CLI** | Rich (terminal UI) |
| **Deployment** | Railway.app |

---

## Project Structure

```
hiredrop/                          # ~4,500 lines total
│
├── web_app.py                    # 2,030 lines — FastAPI app + embedded HTML templates
├── main.py                       #   136 lines — CLI interface (Rich terminal UI)
├── config.py                     #    19 lines — Environment configuration
├── requirements.txt              #    10 lines — Python dependencies
│
├── database/
│   └── db.py                     #   145 lines — SQLite ORM layer
│
├── modules/
│   ├── ai_cover_letter.py        #    99 lines — Claude API cover letter generator
│   ├── filters.py                #    42 lines — Job filtering logic
│   ├── scraper.py                #    33 lines — Legacy scraper (RemoteOK only, used by CLI)
│   ├── telegram_bot.py           #    22 lines — Telegram notifications
│   ├── email_parser.py           #    65 lines — IMAP email checker
│   ├── encryption.py             #    19 lines — Fernet encryption utilities
│   │
│   └── platforms/                # Pluggable scraper system
│       ├── __init__.py           # Re-exports get_enabled_platforms
│       ├── base.py               #    15 lines — Abstract base class
│       ├── registry.py           #    30 lines — Platform registry & loader
│       ├── remoteok.py           #    42 lines — ✅ RemoteOK (JSON API)
│       ├── indeed.py             #    55 lines — ✅ Indeed (HTML scraping)
│       ├── wellfound.py          #    53 lines — ✅ Wellfound (HTML scraping)
│       ├── glassdoor.py          #    10 lines — 🔲 Stub
│       ├── ziprecruiter.py       #    10 lines — 🔲 Stub
│       ├── google_jobs.py        #    10 lines — 🔲 Stub
│       ├── dice.py               #    10 lines — 🔲 Stub
│       ├── toptal.py             #    10 lines — 🔲 Stub
│       ├── hired.py              #    10 lines — 🔲 Stub
│       └── flexjobs.py           #    10 lines — 🔲 Stub
│
├── chrome-extension/             # 1,766 lines total
│   ├── manifest.json             # Manifest V3 config
│   ├── background.js             #   405 lines — Service worker
│   ├── content.js                #   955 lines — Indeed automation (3-phase)
│   ├── popup.js                  #   266 lines — Extension popup logic
│   ├── popup.html                #   134 lines — Extension popup UI
│   ├── ping.js                   #     6 lines — Dashboard connection detector
│   └── icons/                    # icon16.png, icon48.png, icon128.png
│
├── data/                         # Runtime data (gitignored)
│   ├── hiredrop.db                # SQLite database (lives in database/ dir actually)
│   ├── profile.json              # User profile
│   ├── resume.pdf                # Uploaded resume
│   ├── campaign_state.json       # Campaign running state
│   ├── connections.json          # Platform connection status
│   └── cookies_remoteok.json     # Legacy cookies
│
└── templates/
    └── cover_letter.txt          # Fallback cover letter template
```

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                           USER                                       │
│                    ┌──────────┐   ┌───────────────┐                  │
│                    │ Web      │   │ Chrome         │                  │
│                    │ Dashboard│   │ Extension      │                  │
│                    └────┬─────┘   └──────┬────────┘                  │
└─────────────────────────┼────────────────┼──────────────────────────┘
                          │                │
                     HTTP │           Chrome│Messages
                          │                │
┌─────────────────────────▼────────────────▼──────────────────────────┐
│                    FastAPI Backend (web_app.py)                       │
│                                                                      │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  ┌───────────────────┐  │
│  │ Profile  │  │ Campaign │  │ Job       │  │ Application       │  │
│  │ Manager  │  │ Manager  │  │ Search    │  │ Tracker           │  │
│  └────┬─────┘  └────┬─────┘  └─────┬─────┘  └────────┬──────────┘  │
│       │              │              │                  │             │
│  ┌────▼─────┐  ┌─────▼─────┐  ┌────▼──────┐  ┌───────▼──────────┐  │
│  │profile   │  │campaign   │  │Platform   │  │SQLite DB         │  │
│  │.json     │  │_state.json│  │Scrapers   │  │(jobs,applications)│  │
│  └──────────┘  └───────────┘  └────┬──────┘  └──────────────────┘  │
│                                    │                                │
│       ┌────────────────────────────┼──────────┐                     │
│       │              │             │          │                     │
│  ┌────▼────┐  ┌──────▼───┐  ┌─────▼───┐  ┌──▼──────┐              │
│  │RemoteOK │  │Indeed    │  │Wellfound│  │Stubs   │              │
│  │(API)    │  │(scrape)  │  │(scrape) │  │(7 more)│              │
│  └─────────┘  └──────────┘  └─────────┘  └────────┘              │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │                    Integrations                              │    │
│  │  ┌──────────┐  ┌────────────┐  ┌──────────┐  ┌──────────┐  │    │
│  │  │Claude AI │  │Telegram Bot│  │Gmail IMAP│  │Filters   │  │    │
│  │  │(cover    │  │(notify)    │  │(check    │  │(keyword, │  │    │
│  │  │ letters) │  │            │  │ replies) │  │ dedup)   │  │    │
│  │  └──────────┘  └────────────┘  └──────────┘  └──────────┘  │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                 Chrome Extension (Indeed.com)                         │
│                                                                      │
│  ┌────────────┐    Chrome Messages    ┌──────────────────────────┐  │
│  │ popup.js   │◄──────────────────────│ background.js            │  │
│  │ (UI,       │                       │ (API calls, state,       │  │
│  │  polling)  │                       │  campaign control)       │  │
│  └────────────┘                       └──────────┬───────────────┘  │
│                                                  │                  │
│                                       Chrome Messages               │
│                                                  │                  │
│                                       ┌──────────▼───────────────┐  │
│                                       │ content.js               │  │
│                                       │ (Indeed page automation) │  │
│                                       │                          │  │
│                                       │ Phase 1: Scan job list   │  │
│                                       │ Phase 2: Extract + AI CL │  │
│                                       │ Phase 3: Fill form       │  │
│                                       └──────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Database Schema

**File:** `database/hiredrop.db` (SQLite)

### Table: `jobs`

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | INTEGER | AUTO | Primary key |
| `title` | TEXT | NOT NULL | Job title |
| `company` | TEXT | NOT NULL | Company name |
| `link` | TEXT | NOT NULL | Job URL (used for deduplication) |
| `status` | TEXT | `'new'` | `new` / `applied` / `interview` / `rejected` |
| `date_found` | TEXT | NOT NULL | ISO 8601 timestamp |
| `platform` | TEXT | `'remoteok'` | Source platform name |
| `description` | TEXT | `''` | Job description text |
| `location` | TEXT | `''` | Job location |
| `job_type` | TEXT | `''` | `full-time` / `part-time` / `contract` |

### Table: `applications`

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | INTEGER | AUTO | Primary key |
| `job_id` | INTEGER | NOT NULL | Foreign key → `jobs.id` |
| `date_applied` | TEXT | NOT NULL | ISO 8601 timestamp |
| `status` | TEXT | `'applied'` | `applied` / `under review` / `interview` / `rejected` |
| `cover_letter` | TEXT | `''` | Full text of submitted cover letter |

### Migration Strategy

`init_db()` runs on every startup. Uses `ALTER TABLE ... ADD COLUMN` wrapped in try/except to add new columns to existing tables without data loss. No migration framework (Alembic, etc.) — manual column additions.

### Key Queries

- **Deduplication:** `SELECT 1 FROM jobs WHERE link = ?` — prevents saving duplicate jobs
- **Today's applications by platform:** JOIN `applications` + `jobs`, group by `platform`, filter by today's date
- **Stats:** Simple COUNT queries on both tables

---

## Backend (FastAPI)

**Entry point:** `web_app.py` — single file containing the FastAPI app, all route handlers, Pydantic models, HTML templates, and helper functions.

**Server:** Uvicorn (ASGI)

**CORS:** Configured to allow requests from any `chrome-extension://` origin:
```python
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^chrome-extension://.*$",
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
```

### API Endpoints

#### Profile

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/profile` | Get user profile (adds defaults for `last_name`, `phone`) |
| `POST` | `/api/profile` | Update profile (preserves `platform_credentials`) |

#### Job Search

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/find-jobs` | Scrape jobs from enabled platforms, filter, save to DB, notify via Telegram |
| `GET` | `/api/jobs` | List all saved jobs (ordered by `date_found` DESC) |

#### Cover Letters

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/cover-letter` | Generate cover letter for a specific job by `job_id` |
| `POST` | `/api/cover-letter-preview` | Generate sample cover letter from keywords (used by extension) |
| `POST` | `/api/cover-letter-template` | Save custom fallback template to `templates/cover_letter.txt` |

#### Applications

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/application/save` | Save completed application (called by Chrome extension) |
| `GET` | `/api/applications/history` | Last 50 applications with job details |

#### Campaign Management

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/campaign/start` | Mark campaign as started (saves filters + timestamp) |
| `POST` | `/api/campaign/stop` | Mark campaign as stopped |
| `GET` | `/api/campaign/status` | Get campaign state, today's counts, jobs ready |

#### Platform Connections

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/connections` | Connection state for all connectable platforms |
| `POST` | `/api/connections/connect` | Mark platform as connected |
| `POST` | `/api/connections/disconnect` | Mark platform as disconnected |
| `GET` | `/api/platform/inbox-urls` | Links to platform message inboxes |

#### Resume

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/upload-resume` | Upload resume PDF (only `.pdf` accepted) |
| `GET` | `/api/resume-status` | Check if resume is uploaded |
| `GET` | `/api/resume-download` | Download resume (used by extension for form upload) |

#### Other

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard HTML (or onboarding wizard if no `profile.json`) |
| `GET` | `/api/stats` | Total jobs, total applications, new today |
| `GET` | `/api/checklist` | Setup completion status (resume, keywords, search) |
| `GET` | `/api/email-check` | Check IMAP for job-related emails, notify via Telegram |
| `GET` | `/api/extension/download` | Zip and download chrome-extension folder |

### Data Storage

All persistent user data is stored as JSON files in the `data/` directory:

| File | Format | Purpose |
|------|--------|---------|
| `profile.json` | JSON | User profile: name, last_name, email, phone, keywords, location, job_type, platforms, writing_style, platform_credentials |
| `campaign_state.json` | JSON | Campaign: running (bool), filters (obj), started_at (ISO timestamp) |
| `connections.json` | JSON | Platform connections: `{platform: {connected: bool, connected_at: timestamp}}` |
| `resume.pdf` | Binary | User's resume PDF |

### Pydantic Models

```python
class ProfileUpdate:
    name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    keywords: List[str] = []
    location: str = "remote"  # "remote", "usa", "europe", or custom
    job_type: str = "full-time"  # "full-time", "part-time", "contract"
    platforms: List[str] = ["remoteok"]
    writing_style: str = ""  # Free text: user's writing sample for AI to match


class ApplicationSaveRequest:
    job_title: str
    company: str
    platform: str = ""
    job_url: str = ""
    cover_letter: str = ""
    status: str = "applied"


class CampaignStartRequest:
    keywords: List[str] = []
    platforms: List[str] = []
    location: str = ""
    job_type: str = ""


class CoverLetterRequest:
    job_id: int


class LetterPreviewRequest:
    keywords: str


class TemplateRequest:
    template: str


class FindJobsRequest:
    platforms: List[str] = []


class ConnectPlatformRequest:
    platform: str
```

---

## Platform Scrapers

### Scraper Architecture

All platform scrapers follow a plugin pattern:

```python
# base.py — Abstract interface
class JobPlatform(ABC):
    name = ""  # Internal ID: "remoteok", "indeed", etc.
    display_name = ""  # Human-readable: "RemoteOK", "Indeed", etc.

    @abstractmethod
    def scrape(self, keywords=None, location="remote", max_results=25):
        """Return list of normalized job dicts."""
        pass
```

**Normalized job dict format** (every scraper must return this):

```python
{
    "title": str,  # Job title
    "company": str,  # Company name
    "link": str,  # Full URL to job posting
    "date": str,  # Date posted (if available)
    "platform": str,  # Platform name (e.g., "indeed")
    "location": str,  # Location text
    "job_type": str,  # "full-time", "part-time", "contract"
    "tags": list[str],  # Skill/technology tags
    "description": str,  # Job description text
}
```

**Registry** (`registry.py`):

```python
PLATFORMS = {
    "remoteok": RemoteOKPlatform,
    "indeed": IndeedPlatform,
    "wellfound": WellfoundPlatform,
    "glassdoor": GlassdoorPlatform,  # stub
    "ziprecruiter": ZipRecruiterPlatform,  # stub
    # ... etc
}


def get_enabled_platforms(profile):
    enabled = profile.get("platforms", ["remoteok"])
    return [PLATFORMS[p]() for p in enabled if p in PLATFORMS]
```

### Implemented Platforms

#### RemoteOK
- **Method:** JSON API (`https://remoteok.com/api`)
- **Auth:** None (public API)
- **Note:** First item in response is metadata, skipped via `data[1:]`
- **Always available** — no "connection" required

#### Indeed
- **Method:** HTML scraping (`https://www.indeed.com/jobs?q=...&l=...&limit=...`)
- **Auth:** None (public pages)
- **Parser:** BeautifulSoup4, selects `.job_seen_beacon` cards
- **Requires:** Platform connection (`connections.json`)

#### Wellfound
- **Method:** HTML scraping (`https://wellfound.com/role/r/{keywords}`)
- **Auth:** None
- **Parser:** BeautifulSoup4, selects `.styles_result__rPRNG` cards
- **Requires:** Platform connection

### Adding a New Platform

1. Create `modules/platforms/newplatform.py`:
```python
from modules.platforms.base import JobPlatform


class NewPlatform(JobPlatform):
    name = "newplatform"
    display_name = "New Platform"

    def scrape(self, keywords=None, location="remote", max_results=25):
        # Fetch and parse jobs
        # Return list of normalized job dicts
        return jobs
```

2. Register in `modules/platforms/registry.py`:
```python
from modules.platforms.newplatform import NewPlatform

PLATFORMS["newplatform"] = NewPlatform
```

3. If the platform requires login/connection, add to `CONNECTABLE_PLATFORMS` in `web_app.py`.

---

## Job Filtering

**Module:** `modules/filters.py`

**Input:** Raw list of job dicts from scrapers
**Output:** Filtered list (no duplicates, matching keywords)

### Filter chain:

1. **Deduplication** — `job_exists(link)` checks if URL already in DB
2. **Keyword match** — At least one keyword must appear in: `title` + `tags` + `description` (case-insensitive)
3. **Location filter** — If user wants "remote", skip jobs with explicit non-remote locations
4. **Job type filter** — If user set a preference, skip non-matching types

```python
def filter_jobs(jobs):
    profile = load_profile()
    keywords = [k.lower() for k in profile.get("keywords", [])]
    # ... filters applied sequentially
```

**Important:** Keywords use `any()` match (OR logic) — a job matches if it contains ANY of the keywords.

---

## AI Cover Letter Generation

**Module:** `modules/ai_cover_letter.py`

### Flow

1. Load user profile (name, email, writing_style)
2. Extract resume text via pdfplumber (first 2000 chars)
3. Build system prompt with strict rules
4. Call Claude API
5. On failure → use fallback template

### System Prompt (Key Rules)

```
- Sound like the person wrote it themselves, NOT like an AI assistant
- NO buzzwords: leverage, passionate, synergy, excited to apply, thrilled
- NO formal openers like "I am writing to express my interest"
- Short paragraphs. Max 2-3 sentences each.
- Max 120 words total.
- Be direct: what you did, why this job, one specific thing about the company.
- Tell a micro-story, NOT skills list.
- If writing_style provided: match tone, vocabulary, rhythm exactly.
```

### API Call

```python
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
message = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=512,
    system=system_prompt,
    messages=[{"role": "user", "content": prompt}],
)
```

### Prompt Template

```
Write a cover letter for this job application.

Job Title: {title}
Company: {company}
Job Description: {description[:500]}

Applicant Name: {name}
Applicant Email: {email}

Candidate background (from resume):
{resume_text or "Not provided."}
```

### Fallback

If Claude API fails or `ANTHROPIC_API_KEY` not set:
1. Read `templates/cover_letter.txt`
2. Replace `{company}` and `{title}` variables
3. Return result (or empty string if template missing)

---

## Chrome Extension

### Extension Architecture

The Chrome extension is a Manifest V3 extension with three main components that communicate via Chrome's messaging API:

```
popup.html/popup.js  ←── User interface (360px popup)
       │
       │ chrome.runtime.sendMessage
       ▼
background.js        ←── Service worker (API calls, state management)
       │
       │ chrome.runtime.sendMessage
       ▼
content.js           ←── Page automation (injected into indeed.com)
```

### Manifest & Permissions

```json
{
  "manifest_version": 3,
  "permissions": ["activeTab", "storage", "scripting", "tabs", "alarms"],
  "host_permissions": [
    "https://web-production-db45.up.railway.app/*",
    "https://*.indeed.com/*"
  ],
  "content_scripts": [
    {
      "matches": ["https://*.indeed.com/*"],
      "js": ["content.js"],
      "run_at": "document_idle"
    },
    {
      "matches": ["http://localhost:*/*", "https://web-production-db45.up.railway.app/*"],
      "js": ["ping.js"],
      "run_at": "document_idle"
    }
  ]
}
```

- `content.js` is injected on **all indeed.com pages**
- `ping.js` is injected on the **dashboard** (localhost or Railway) — enables extension detection from the web app via `window.postMessage("HIREDROP_PING")`

### Background Service Worker

**File:** `chrome-extension/background.js` (405 lines)

**Responsibilities:**
- **API communication** — All `fetch()` calls to the FastAPI backend go through here
- **Profile caching** — Fetches profile from API, caches in `chrome.storage.local` with 5-minute TTL
- **Campaign state** — Tracks `campaignRunning`, `campaignTabId`, `campaignStartedAt`
- **Application counter** — `todayCount` and `platformCounts`, resets on date change
- **Activity log** — Maintains last 50 log entries in storage
- **Badge** — Updates extension badge (count + color: green when running, purple when idle)
- **Tab lifecycle** — If campaign tab is closed, auto-stops campaign

**API Base URL:** `https://web-production-db45.up.railway.app`

**Key constants:**
- `LIMIT_PER_PLATFORM = 50` — max applications per platform per day

**Indeed URL builder:**
```javascript
function buildIndeedUrl(keywords, location, jobType) {
    // Maps: location → "United States"/"remote"/""
    // Maps: job_type → "fulltime"/"parttime"/"contract"
    // Always adds: iafilter=1 (Easy Apply filter)
    return `https://www.indeed.com/jobs?${params}`;
}
```

### Content Script — 3-Phase Automation

**File:** `chrome-extension/content.js` (955 lines)

The content script runs on all indeed.com pages and operates in three phases. It uses a `MutationObserver` to detect SPA navigation and phase transitions.

#### Phase Detection

```javascript
function detectPhase() {
    if (isFormVisible()) return "form";        // Phase 3: apply form/modal visible
    if (url.includes("/viewjob")) return "detail";  // Phase 2: job detail
    if (url.includes("/jobs?")) return "list";  // Phase 1: search results
    return "unknown";
}
```

#### Phase 1: Job List Page (`/jobs?`)

1. Wait 2-3 seconds for cards to load
2. Find job cards using multiple selectors (Indeed changes HTML frequently):
   ```javascript
   const JOB_CARD_SELECTORS = [
       ".job_seen_beacon",
       ".resultContent",
       ".jobsearch-ResultsList li",
       "[data-jk]",
       'div[class*="cardOutline"]',
       'td.resultContent',
   ];
   ```
3. Filter for "Easily apply" cards (regex: `/easily\s*apply/i`)
4. Skip already-applied URLs (tracked in `chrome.storage.local`, last 500)
5. Save pending jobs to storage
6. Click first job card to enter Phase 2
7. If no Easy Apply jobs found → navigate to next page

#### Phase 2: Job Detail Page (`/viewjob`)

1. Extract job info:
   - Title (multiple selectors for different Indeed layouts)
   - Company name
   - Description (first 1000 chars)
2. Verify it's an "Easily apply" job
3. Save job context to storage
4. Request cover letter generation via `GENERATE_COVER_LETTER` message → background.js → API
   - 15-second timeout with fallback
5. Save generated cover letter to storage
6. Find and click Apply button:
   ```javascript
   const selectors = [
       'button[id*="indeedApply"]',
       ".ia-IndeedApplyButton",
       'button[class*="IndeedApply"]',
       'button[aria-label*="Apply now"]',
       // ... text-based fallback
   ];
   ```

#### Phase 3: Application Form (multi-step)

1. Loop through form steps (max 8 iterations, safety limit)
2. On each step, detect and fill visible fields:

   | Field | Selectors | Typing Method |
   |-------|-----------|---------------|
   | First name | `input[name*="firstName"]`, `input[autocomplete="given-name"]` | `typeValue()` (char-by-char) |
   | Last name | `input[name*="lastName"]`, `input[autocomplete="family-name"]` | `typeValue()` (char-by-char) |
   | Email | `input[type="email"]`, `input[name*="email"]` | `typeValue()` (char-by-char) |
   | Phone | `input[type="tel"]`, `input[name*="phone"]` | `typeValue()` (char-by-char) |
   | Cover letter | `textarea[name*="coverletter"]` | `quickSet()` (instant) |
   | Resume | `input[type="file"]` near "resume"/"cv" text | File upload via DataTransfer API |

3. Resume upload flow:
   ```javascript
   // Fetch PDF from backend
   const res = await fetch(`${API_BASE}/api/resume-download`);
   const blob = await res.blob();
   const file = new File([blob], "resume.pdf", { type: "application/pdf" });
   // Set via DataTransfer
   const dt = new DataTransfer();
   dt.items.add(file);
   fileInput.files = dt.files;
   ```

4. Detect submit step:
   - Looks for buttons containing "Submit application" text
   - If found → click submit → report `APPLICATION_SAVED` → go back to job list

5. If not submit step → click Continue/Next → wait for next step

6. After submission:
   - Sends `APPLICATION_SAVED` to background.js
   - Adds URL to applied list
   - Waits 4-6 seconds
   - Returns to job list (with incremented `start` param to skip seen jobs)

### Popup UI

**Files:** `popup.html` (134 lines), `popup.js` (266 lines)

**Layout:** 360px wide, dark theme

**Sections:**
1. **Header** — "HireDrop v1.0" logo
2. **Connection row** — Green/red dot + "Connected"/"Offline" text + profile name
3. **Stats grid** — 3 boxes: Today | This Week | Total
4. **Campaign section** — Start/Stop button, elapsed time, current job display
5. **Activity log** — Last 10 events with timestamps
6. **Dashboard button** — Opens web app in new tab

**Polling:** `loadStatus()` every 2 seconds via `setInterval`

### Message Protocol

All communication uses `chrome.runtime.sendMessage()` with `type` field.

#### Popup → Background

| Message Type | Payload | Response |
|-------------|---------|----------|
| `GET_PROFILE` | — | Profile object |
| `REFRESH_PROFILE` | — | Profile object |
| `CHECK_CONNECTION` | — | `{connected: bool}` |
| `START_CAMPAIGN` | `{filters: {...}}` | `{started: true, tabId, url}` |
| `STOP_CAMPAIGN` | — | `{stopped: true}` |
| `GET_STATUS` | — | Full status object (campaign state, counts, stats) |

#### Content Script → Background

| Message Type | Payload | Response |
|-------------|---------|----------|
| `APPLICATION_SAVED` | `{data: {job_title, company, platform, job_url, cover_letter, status}}` | `{saved: bool, todayCount, platformCount, job_id}` |
| `GENERATE_COVER_LETTER` | `{data: {job_title, company, description}}` | `{letter, source, job_title, company}` |
| `STEP_FAILED` | `{data: {phase, error}}` | `{ok: true}` |
| `LOG` | `{text, cls}` | — (fire-and-forget) |

#### Background → Content Script

| Message Type | When |
|-------------|------|
| `CAMPAIGN_STARTED` | After campaign starts |
| `CAMPAIGN_STOPPED` | After campaign stops |

### Anti-Detection Techniques

1. **Character-by-character typing** with random delays (50-150ms):
   ```javascript
   async function typeValue(el, value) {
       for (let i = 0; i < value.length; i++) {
           setNativeValue(el, value.slice(0, i + 1));
           await sleep(rand(50, 150));
       }
   }
   ```

2. **Random delays between actions** — `sleep(rand(2000, 5000))` between page transitions

3. **React-compatible form filling** — Uses native property setters to bypass React's controlled inputs:
   ```javascript
   function setNativeValue(el, value) {
       const setter = Object.getOwnPropertyDescriptor(
           HTMLInputElement.prototype, "value"
       )?.set;
       setter.call(el, value);
       el.dispatchEvent(new Event("input", { bubbles: true }));
       el.dispatchEvent(new Event("change", { bubbles: true }));
   }
   ```

4. **Realistic User-Agent** in scraper requests:
   ```
   Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36
   ```

5. **Daily limits** — 50 applications per platform per day

---

## Telegram Notifications

**Module:** `modules/telegram_bot.py` (22 lines)

**API:** `https://api.telegram.org/bot{TOKEN}/sendMessage`

**Configuration:** `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` from `.env`

**Trigger points:**
1. After `POST /api/find-jobs` — `"HireDrop: {count} new jobs found on {platforms}!"`
2. After `GET /api/email-check` — `"HireDrop Email: {subject}\nFrom: {sender}"` (for each match)

**Behavior:** Silently fails if not configured (prints warning to console).

**Parse mode:** HTML — supports `<b>`, `<i>`, `<a>` tags in messages.

---

## Email Monitoring

**Module:** `modules/email_parser.py` (65 lines)

**Protocol:** IMAP4 over SSL

**Default server:** `imap.gmail.com` (configurable via `EMAIL_IMAP_SERVER`)

### Logic

1. Connect to IMAP server
2. Login with `EMAIL_ADDRESS` / `EMAIL_PASSWORD`
3. Search for `UNSEEN` emails in inbox
4. For each unseen email, check if subject contains any keywords:
   - `"interview"`
   - `"application received"`
   - `"next step"`
   - `"thank you for applying"`
5. Uses `BODY.PEEK[]` (does NOT mark emails as read)
6. Returns matching emails: `{subject, sender, date}`

### Header Decoding

Handles multi-part encoded headers (RFC 2047) with charset detection:
- Tries declared charset
- Falls back to UTF-8
- Uses `errors="replace"` for robustness

---

## Encryption Module

**Module:** `modules/encryption.py` (19 lines)

**Algorithm:** Fernet (AES-128-CBC + HMAC-SHA256)

**Functions:**
- `get_key()` — reads `ENCRYPTION_KEY` from env, generates new key if missing (prints warning)
- `encrypt_password(text) → str` — encrypts plaintext to base64 token
- `decrypt_password(token) → str` — decrypts token to plaintext

**Current status:** Module exists but is NOT used anywhere in the codebase. Prepared for future use (storing platform credentials securely).

---

## Frontend (Dashboard & Onboarding)

The frontend consists of two HTML pages embedded directly as Python string templates in `web_app.py` (~1,500 lines of HTML/CSS/JS):

### Route logic:

```python
@app.get("/")
def dashboard():
    if not os.path.exists(PROFILE_PATH):
        return ONBOARDING_HTML  # First-time setup wizard
    return DASHBOARD_HTML  # Main dashboard
```

### Onboarding Wizard (`ONBOARDING_HTML`)

Multi-step setup wizard:
1. **Profile** — Name, email, phone
2. **Keywords** — Job search keywords
3. **Platforms** — Select which platforms to search
4. **Resume** — Upload PDF
5. **Writing style** — Sample text for AI to match tone
6. **Preview** — AI generates sample cover letter
7. **Chrome Extension** — Download extension instructions

Uses Intro.js for tour/tooltip guidance.

### Dashboard (`DASHBOARD_HTML`)

Single-page app with:
- Stats overview (total jobs, applications, new today)
- Job listings table with status badges and filter tabs
- Cover letter generation per job
- Application history
- Platform connection management
- Campaign status
- Email check trigger
- Extension download

**Design:** Dark theme, CSS custom properties, responsive layout.

**API calls:** Vanilla JS `fetch()` to backend endpoints.

---

## CLI Interface

**File:** `main.py` (136 lines)

Alternative terminal-based interface using Rich library. Menu-driven:

```
[1] Find Jobs         → Scrapes RemoteOK only (legacy scraper)
[2] View Saved Jobs   → Table of all jobs in DB
[3] Generate Cover Letter → Select job by ID, generate via AI
[4] Check Email Responses → IMAP check + table display
[5] Exit
```

**Note:** CLI uses the legacy `modules/scraper.py` (RemoteOK only), not the pluggable platform system. The web app uses the new platform system.

---

## Deployment

**Platform:** Railway.app

**Production URL:** `https://web-production-db45.up.railway.app`

This URL is hardcoded in:
- `chrome-extension/manifest.json` → `host_permissions`
- `chrome-extension/background.js` → `API_BASE`
- `chrome-extension/content.js` → `API_BASE`
- `chrome-extension/popup.js` → `API_BASE`

**Run command:** `uvicorn web_app:app --host 0.0.0.0 --port $PORT`

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | For AI features | `""` | Anthropic API key for cover letter generation |
| `TELEGRAM_BOT_TOKEN` | For notifications | `""` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | For notifications | `""` | Telegram chat ID for receiving messages |
| `EMAIL_ADDRESS` | For email monitoring | `""` | IMAP email address |
| `EMAIL_PASSWORD` | For email monitoring | `""` | IMAP email password (Gmail: app password) |
| `EMAIL_IMAP_SERVER` | No | `"imap.gmail.com"` | IMAP server hostname |
| `ENCRYPTION_KEY` | No | Auto-generated | Fernet encryption key (not actively used) |

---

## Dependencies

```
fastapi          # Web framework
uvicorn          # ASGI server
requests         # HTTP client (scraping, Telegram)
beautifulsoup4   # HTML parsing (Indeed, Wellfound)
rich             # Terminal UI (CLI mode)
python-dotenv    # .env file loading
anthropic        # Claude AI SDK
python-multipart # File upload support (FastAPI)
pdfplumber       # PDF text extraction (resume)
cryptography     # Fernet encryption
```

---

## Known Limitations & TODOs

### Architecture

- **Frontend embedded in Python** — `web_app.py` is 2,030 lines because HTML/CSS/JS templates are inline strings. Extracting to separate template files would improve maintainability.
- **No authentication** — API endpoints are completely open. Anyone with the URL can access/modify data.
- **No rate limiting** — No protection against API abuse.
- **SQLite** — Single-file database, no connection pooling, not suitable for concurrent access at scale.
- **No async scrapers** — All scraping is synchronous (`requests`), blocks the event loop during `POST /api/find-jobs`.

### Platform Scrapers

- **7 stub platforms** — Glassdoor, ZipRecruiter, Google Jobs, Dice, Toptal, Hired, FlexJobs are registered but return empty lists.
- **Scraper fragility** — HTML scrapers (Indeed, Wellfound) depend on specific CSS classes that change when sites update their frontend.
- **Legacy scraper** — `modules/scraper.py` duplicates RemoteOK functionality and is only used by CLI.

### Chrome Extension

- **Indeed only** — Auto-apply automation only works on Indeed.com.
- **Hardcoded API URL** — Production Railway URL is hardcoded in 4 places (no environment configuration for extension).
- **No retry queue** — If `APPLICATION_SAVED` API call fails, the application is lost.
- **Form handling** — Only handles basic fields (name, email, phone, cover letter, resume). Custom questions, dropdowns, and checkboxes are skipped.

### Other

- **Encryption module unused** — `encryption.py` exists but no code calls it.
- **No tests** — No unit or integration tests.
- **No logging framework** — Uses `print()` statements for debugging.
- **No error tracking** — No Sentry or similar.
- **Profile.json as source of truth** — User data is split between SQLite (jobs/applications) and JSON files (profile, connections, campaign state) with no transactional consistency.
