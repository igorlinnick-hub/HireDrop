"""Backend configuration — env-driven.

All env vars loaded once at import via python-dotenv (reads .env if present).
Sections below group vars by purpose. Add a new var? Put it in the right
section here and mirror it in .env.example.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# AUTH — Supabase (required)
# =============================================================================
# SUPABASE_SERVICE_KEY is the service_role key (NOT anon). Used by backend for
# admin operations + JWT verification via supabase.auth.get_user().
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


# =============================================================================
# AI — Anthropic Claude (required for cover letter generation)
# =============================================================================
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


# =============================================================================
# EMAIL — Resend (transactional, required for password reset)
# =============================================================================
# Railway blocks outbound SMTP on every standard port, so transactional email
# is sent via Resend's HTTPS API. Free tier: 3,000 emails/month.
# RESEND_FROM_EMAIL unset → modules/email_sender.py defaults to
# onboarding@resend.dev (sandbox: delivers only to email verified on the
# Resend account). For production, verify a domain on Resend + set this.
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "")


# =============================================================================
# EMAIL — Gmail IMAP (optional, inbox-checking flow only)
# =============================================================================
# Separate from transactional sending — used by modules/email_parser.py to
# scan a Gmail inbox for recruiter responses. EMAIL_PASSWORD must be a Gmail
# app password (16 chars, generated with 2FA on).
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_IMAP_SERVER = os.getenv("EMAIL_IMAP_SERVER", "imap.gmail.com")


# =============================================================================
# FRONTEND — base URL for links in transactional emails
# =============================================================================
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://jobflow-website-beaa.vercel.app")


# =============================================================================
# GMAIL OAUTH — separate OAuth client for cold-email outreach
# =============================================================================
# Distinct from the login Google OAuth (which is configured in Supabase).
# This one requests `gmail.send` scope so the backend can send mail on the
# user's behalf via the Gmail API. Set up in Google Cloud Console → APIs &
# Services → Credentials → OAuth client ID (web app). Redirect URI:
# {BACKEND_URL}/api/v1/oauth/gmail/callback.
GOOGLE_GMAIL_CLIENT_ID = os.getenv("GOOGLE_GMAIL_CLIENT_ID", "")
GOOGLE_GMAIL_CLIENT_SECRET = os.getenv("GOOGLE_GMAIL_CLIENT_SECRET", "")
# Where Google should send the user back after consent. Defaults to prod;
# override for local dev.
GOOGLE_GMAIL_REDIRECT_URI = os.getenv(
    "GOOGLE_GMAIL_REDIRECT_URI",
    "https://web-production-db45.up.railway.app/api/v1/oauth/gmail/callback",
)


# =============================================================================
# TIERS / RATE LIMITS
# =============================================================================
# Comma-separated emails in ADMIN_EMAILS bypass tier limits + cover-letter
# rate limit. Lowercased on read so checks are case-insensitive.
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.getenv("ADMIN_EMAILS", "").split(",")
    if e.strip()
}

# Cover letter generation rate limit. Soft mode (default) just counts;
# hard mode (RATE_LIMIT_ENFORCE=true) returns 429 once the daily count is hit.
RATE_LIMIT_LETTERS_PER_DAY = int(os.getenv("RATE_LIMIT_LETTERS_PER_DAY", "50"))
RATE_LIMIT_ENFORCE = os.getenv("RATE_LIMIT_ENFORCE", "false").lower() in ("1", "true", "yes")


# =============================================================================
# DEFAULTS (fallback values used by legacy code paths)
# =============================================================================
SEARCH_KEYWORDS = ["marketing", "content", "automation"]
LOCATION = "remote"
MAX_JOBS = 25
