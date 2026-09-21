-- Migration: hand-backs — the applications the filler could NOT finish, surfaced as a
-- live to-do instead of an archived log line.
--
-- Why a table and not the activity log: the log already carries these as metadata
-- (type=handback), but a log line cannot be RESOLVED. Igor, 09-21: "это нужно не в
-- history а в попапе, чтоб была анимация заявки что нужно человеку доделать". Two
-- surfaces now read this (the extension popup and the dashboard rail badge), so the
-- open/done state needs one home both can agree on — a count that differs between the
-- popup and the dashboard is worse than no count at all.
--
-- Run: supabase db query --linked -f migrations/add_handbacks.sql

CREATE TABLE IF NOT EXISTS public.handbacks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL,
  job_title TEXT NOT NULL DEFAULT '',
  company TEXT NOT NULL DEFAULT '',
  -- Where the human resumes. Everything before this screen is already filled in.
  url TEXT NOT NULL DEFAULT '',
  platform TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- NULL = still waiting for the human. Set when they confirm they submitted it, so
  -- the list drains instead of growing into another thing nobody reads.
  resolved_at TIMESTAMPTZ DEFAULT NULL
);

-- The only query either surface makes: this user's open hand-backs, newest first.
CREATE INDEX IF NOT EXISTS handbacks_open_idx
  ON public.handbacks (user_id, created_at DESC)
  WHERE resolved_at IS NULL;

-- Re-opening the same job must not stack duplicates: one open row per job URL.
CREATE UNIQUE INDEX IF NOT EXISTS handbacks_one_open_per_url
  ON public.handbacks (user_id, url)
  WHERE resolved_at IS NULL;

-- Backend uses the service role; no anon access needed. Every query still filters
-- user_id explicitly — service_role bypasses RLS, so RLS is not the guard here.
ALTER TABLE public.handbacks ENABLE ROW LEVEL SECURITY;
