-- Migration: interview kits — the prep sheet we generate for ONE application once its
-- status turns into an interview. Generation costs an AI call, so the result is cached
-- here forever: the kit is a snapshot of what the user was preparing for, and it must
-- stay readable long after the jobs row is cleaned up.
--
-- One kit per (user, application). `payload` holds the whole kit as JSON so the shape can
-- evolve without a migration; `schema_version` lets the reader reject a payload it is too
-- old to render instead of crashing on a missing key.
--
-- Idempotent, safe to re-run.
-- Apply with: supabase db query --linked -f migrations/2026-09-05_interview_kits.sql

CREATE TABLE IF NOT EXISTS public.interview_kits (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL,
  application_id UUID NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}',
  schema_version INT NOT NULL DEFAULT 1,
  model TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, application_id)
);

-- Every read is scoped by user_id (the backend runs under service_role and bypasses RLS,
-- so this index backs the filter that is our only IDOR defense).
CREATE INDEX IF NOT EXISTS interview_kits_user_idx ON public.interview_kits (user_id);

-- Backend uses the service role; no anon access needed.
ALTER TABLE public.interview_kits ENABLE ROW LEVEL SECURITY;
