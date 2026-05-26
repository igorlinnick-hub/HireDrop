# HireDrop

AI-powered job-application automation. Backend on Railway, dashboard on Vercel, the actual auto-apply logic in a Chrome extension that drives Indeed forms.

## Architecture

```
┌─────────────────────┐     ┌──────────────────────┐
│  hiredrop-website    │     │  Chrome extension    │
│  (Vercel, Next.js)  │     │  (Manifest V3)       │
│  — login, dashboard │     │  — Indeed auto-apply │
└──────────┬──────────┘     └───────────┬──────────┘
           │ Bearer JWT                 │ Bearer JWT
           ▼                            ▼
       ┌─────────────────────────────────────┐
       │  Backend (FastAPI on Railway)       │
       │  app/main.py                        │
       │  └─ /api/v1/{profile, jobs, …}      │
       └──────────────────┬──────────────────┘
                          │ supabase-py
                          ▼
                   ┌──────────────┐
                   │   Supabase   │
                   │  Postgres +  │
                   │  Storage +   │
                   │  Auth (JWT)  │
                   └──────────────┘
```

## Repo layout

```
app/                       FastAPI backend (the only entrypoint in prod)
├── main.py                Mounts routers under /api/v1
├── deps.py                get_current_user — Supabase JWT auth
├── schemas.py             Pydantic request/response models
├── db/                    One owner per Supabase table / bucket
│   ├── client.py          Supabase singleton
│   ├── jobs.py            scraped jobs CRUD + dedup
│   ├── applications.py    submission history
│   ├── campaign.py        per-user campaign state
│   ├── profile.py         user settings + connections
│   ├── usage.py           per-user/per-day cover-letter quota
│   ├── activity.py        observability log
│   ├── selectors.py       DOM selectors (data, not code)
│   └── resume.py          resumes bucket gateway
└── routers/               One file per resource group, all under /api/v1

modules/
├── filters.py             Pure keyword/location/job_type filter
├── ai_cover_letter.py     Anthropic singleton + cover-letter pipeline
├── platforms/             One scraper per platform (remoteok, indeed, wellfound)
├── telegram_bot.py        Notification gateway
└── email_parser.py        IMAP response checker

chrome-extension/          Manifest V3 extension that drives Indeed
├── manifest.json
├── background.js          Service worker, API gateway for content.js
├── content.js             Three-phase auto-apply state machine + anti-detect
├── anti_detect/           UA pool + fingerprint helpers (Phase 5)
└── popup.{html,js}        Status UI

tests/                     pytest smoke + unit tests
supabase-schema-v3.sql     DB migrations (idempotent, applied via Management API)
SUPABASE_MIGRATION_v3.md   Runbook for the schema-v3 migration
pyproject.toml             ruff + pyright + pytest config
```

## Endpoints (under `/api/v1/*`, all Bearer-required)

| Method | Path | Purpose |
|---|---|---|
| GET | `/profile` | User settings |
| POST | `/profile` | Update settings |
| POST | `/profile/resume` | Upload PDF (Supabase Storage) |
| GET | `/profile/resume/status` | Has the user uploaded a resume? |
| GET | `/profile/resume/url` | Signed URL, TTL 1h |
| GET | `/jobs` | Saved jobs |
| POST | `/jobs/find` | Scrape + filter + persist |
| GET | `/applications/history` | Submitted applications |
| POST | `/applications/save` | Record a submission (extension) |
| GET | `/stats` | Counter dashboard |
| GET | `/campaign/status` | Active campaign state |
| POST | `/campaign/{start,stop}` | Campaign lifecycle |
| POST | `/tools/cover-letter` | Generate for a saved job |
| POST | `/tools/cover-letter-preview` | Generate ad-hoc |
| GET | `/extension/selectors/{platform}` | DOM selector blob (Phase 4.1) |
| POST | `/activity` | Extension writes events |
| GET | `/activity` | UI reads recent events |
| GET | `/health` | Liveness, no auth |

## Environment

Required:
- `ANTHROPIC_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY` (service_role, **not** anon)

Optional:
- `RATE_LIMIT_LETTERS_PER_DAY` (default `50`)
- `RATE_LIMIT_ENFORCE` (default `false` — soft mode counts but doesn't block)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `EMAIL_ADDRESS`, `EMAIL_PASSWORD`, `EMAIL_IMAP_SERVER`

## Local development

```bash
git clone https://github.com/igorlinnick-hub/HireDrop
cd HireDrop
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env  # fill in the keys
uvicorn app.main:app --reload --port 8000
```

Run the test suite:

```bash
pytest tests/        # 31 tests, ~1s
ruff check .         # lint
ruff format .        # format
pyright app modules  # type check (informational)
```

## Deployment

- **Backend**: Railway auto-deploys on every push to `main`. `Procfile` runs `uvicorn app.main:app`. Env vars in Railway dashboard.
- **Frontend**: Vercel auto-deploys `igorlinnick-hub/hiredrop-website` (separate repo).
- **Extension**: load unpacked from `chrome-extension/` for development. Distribution via Chrome Web Store on release.
- **Schema migrations**: SQL in `supabase-schema-vN.sql`. Apply via Management API (PAT in `.env` as `SUPABASE_PAT`) — see `SUPABASE_MIGRATION_v3.md`.

## Anti-detect (Phase 5)

The extension implements behavioural anti-detect for Indeed:
- Detection backoff — captcha/challenge pages stop the campaign immediately
- Log-normal action delays (long tail, not uniform 2-3s)
- 5% misclick rate with corrective re-click
- Bezier mouse trails before each click, with `mousedown`/`mouseup` brackets
- 10-30s session warmup before the first action
- One UA from a curated pool per session, installed via `declarativeNetRequest`

Detection patterns and DOM selectors live in the `platform_selectors` table and are refreshed by the extension every 24h. Updating Indeed selectors is a SQL UPDATE, not a release.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md).
