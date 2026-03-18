# JobFlow

AI-powered job search automation. Finds jobs across platforms, generates personalized cover letters, and tracks applications.

## Quickstart

```bash
git clone <repo-url> && cd jobflow
pip install -r requirements.txt
playwright install chromium
cp .env.example .env        # add your ANTHROPIC_API_KEY
python web_app.py
```

Open [http://localhost:8000](http://localhost:8000) — the onboarding wizard will guide you through setup.

## Features

- Multi-platform job search (RemoteOK, Indeed, Wellfound, Glassdoor, and more)
- AI cover letter generation based on your resume and writing style
- Automated application campaigns with daily limits
- Platform connection management via your system browser
- Check responses directly on platform inboxes

## Configuration

Only `ANTHROPIC_API_KEY` is required. See `.env.example` for optional integrations (Telegram notifications, email checking).
