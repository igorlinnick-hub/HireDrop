import os

from dotenv import load_dotenv

load_dotenv()

# API Keys & Tokens
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Admin allowlist — comma-separated emails bypass all tier limits + rate limits.
# Lowercased on read so config and runtime checks are case-insensitive.
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.getenv("ADMIN_EMAILS", "").split(",")
    if e.strip()
}

# Email Settings (IMAP — only for inbox-checking flow; transactional sending
# happens over Resend HTTPS, see below. Railway blocks outbound SMTP.)
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_IMAP_SERVER = os.getenv("EMAIL_IMAP_SERVER", "imap.gmail.com")

# Resend — HTTPS transactional email service (used for password reset etc.).
# Get an API key at https://resend.com/api-keys (free tier: 3000 emails/month).
# RESEND_FROM_EMAIL is the verified sender; in sandbox (no verified domain)
# use onboarding@resend.dev and you can only send to the email verified on
# your Resend account.
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "")

# Frontend origin — used to build auth redirect links in transactional emails.
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://jobflow-website-beaa.vercel.app")

# Supabase (read here too so non-db modules don't reach into app.db.client).
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

# Default Search Filters
SEARCH_KEYWORDS = ["marketing", "content", "automation"]
LOCATION = "remote"
MAX_JOBS = 25
