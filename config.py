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
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://hiredrop.io")


# =============================================================================
# TIERS / RATE LIMITS
# =============================================================================
# Comma-separated emails in ADMIN_EMAILS bypass tier limits + cover-letter
# rate limit. Lowercased on read so checks are case-insensitive.
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}

# Daily AI quota — cover letters, screener answers, AND ATS resume generation all
# draw from this one budget. ENFORCED by default (a 429 is returned once the daily
# count is hit) so a free account / script can't loop an AI endpoint to burn our
# Anthropic spend. Set RATE_LIMIT_ENFORCE=false only to temporarily observe usage
# without blocking anyone.
# Must stay ABOVE the max daily application cap (tap mode = 100/day) or it becomes the
# real bottleneck: a tap user applying to 100 jobs needs 100 cover letters, but a 50-letter
# cap would 429 the second half. 120 gives headroom above the 100 tap cap.
RATE_LIMIT_LETTERS_PER_DAY = int(os.getenv("RATE_LIMIT_LETTERS_PER_DAY", "120"))
RATE_LIMIT_ENFORCE = os.getenv("RATE_LIMIT_ENFORCE", "true").lower() in ("1", "true", "yes")

# Hard ceiling for accounts that bypass every other limit — ADMIN_EMAILS (Igor + devs) and
# observe mode. They were the ONLY accounts in the system with no cap at all: the tier cap,
# the per-platform cap and the 120/day AI cap all short-circuit for them. Promo accounts are
# bounded (a grant without a valid future expiry fails closed to free), so the one way to burn
# Anthropic spend unattended was a forgotten campaign or a test loop on an internal account.
#
# Deliberately NOT the same number as the user cap: the point is to keep testing comfortable
# while making runaway spend impossible. 300/day is ~10x a real user's day and ~$2 at
# $0.007/call. Set ADMIN_AI_DAILY_MAX=0 to restore the old unlimited behaviour.
ADMIN_AI_DAILY_MAX = int(os.getenv("ADMIN_AI_DAILY_MAX", "300"))

# Free taste: lifetime cap on the free tier (FREE_TASTE_PLAN.md). The first N
# applications are the product demo (~$0.02/app AI cost, no ATS tailoring);
# after that the free tier is paywalled — subscribe to keep applying.
FREE_APP_LIMIT = int(os.getenv("FREE_APP_LIMIT", "40"))


# =============================================================================
# OBSERVABILITY — stall watch (the "running but nothing is applied" detector)
# =============================================================================
# There is no Sentry/OTel yet: a campaign that keeps its heartbeat but stops
# producing applications used to be invisible until Igor looked at the dashboard.
# The sweep in app/stall_watch.py closes that hole — it writes a warn line into
# the user's activity log and (if ALERT_EMAIL is set) emails ops via Resend.
#
# ALERT_EMAIL empty → the activity line is still written, nothing is emailed.
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")
STALL_WATCH_ENABLED = os.getenv("STALL_WATCH_ENABLED", "true").lower() in ("1", "true", "yes")
STALL_WATCH_INTERVAL_SECS = int(os.getenv("STALL_WATCH_INTERVAL_SECONDS", "300"))
# Two thresholds, because the two silences mean different things. The FIRST
# application of a run pays for discovery + scraping + scoring + a cover letter,
# so it is legitimately slow; every application after it should follow within
# roughly one form-fill (5-6 min today, see STATUS.md "Троттлинг фонового окна").
STALL_FIRST_MINUTES = int(os.getenv("STALL_FIRST_MINUTES", "45"))
STALL_GAP_MINUTES = int(os.getenv("STALL_GAP_MINUTES", "30"))


# =============================================================================
# BILLING — Stripe (subscriptions)
# =============================================================================
# Secret key + webhook signing secret from the Stripe dashboard. Price IDs are
# the recurring Price objects for each paid plan (create them in Stripe, one per
# tier). All optional at import so the app boots before billing is configured;
# the billing router returns 503 until STRIPE_SECRET_KEY is set.
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
# One paid product (everything, 30/day), billed weekly OR monthly. No free tier,
# no trial, no annual. Create two recurring Prices in Stripe and put the IDs here.
STRIPE_PRICE_WEEKLY = os.getenv("STRIPE_PRICE_WEEKLY", "")
STRIPE_PRICE_MONTHLY = os.getenv("STRIPE_PRICE_MONTHLY", "")


# =============================================================================
# DEFAULTS (fallback values used by legacy code paths)
# =============================================================================
SEARCH_KEYWORDS = ["marketing", "content", "automation"]
LOCATION = "remote"
MAX_JOBS = 25
