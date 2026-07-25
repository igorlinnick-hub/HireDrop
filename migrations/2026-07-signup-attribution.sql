-- Signup attribution (first-touch UTM + referral code) — hiredrop-hq Phase 1.
-- Run in: Supabase Dashboard → SQL Editor (production project). Idempotent.
--
-- Written by the WEBSITE (jobflow-website) under the user's own RLS session:
--   1. SignupForm  — immediate-session path (email confirmation off)
--   2. /auth/callback — email-confirm + Google OAuth paths
-- Both writes are guarded with `attribution is null` → first touch wins,
-- re-logins never overwrite. Shape (jsonb):
--   { utm_source, utm_medium, utm_campaign, utm_content, ref,
--     landing_page, captured_at }
-- `ref` = partner referral code (hiredrop.io/?ref=luca) — consumed by the
-- future referral system; utm_campaign carries the video id from hiredrop-hq.
--
-- Read side: hiredrop-hq `hiredrop-funnel` connector aggregates signups by
-- attribution->>'utm_source' / ->>'ref' (read-only counts).

alter table public.profiles
  add column if not exists attribution jsonb,
  add column if not exists attributed_at timestamptz;

comment on column public.profiles.attribution is
  'First-touch marketing attribution captured at signup: {utm_source, utm_medium, utm_campaign, utm_content, ref, landing_page, captured_at}. Written once (first touch wins).';

-- Fast filter for per-partner and per-campaign rollups in hiredrop-hq.
create index if not exists profiles_attribution_ref_idx
  on public.profiles ((attribution->>'ref'))
  where attribution is not null;

create index if not exists profiles_attribution_source_idx
  on public.profiles ((attribution->>'utm_source'))
  where attribution is not null;
