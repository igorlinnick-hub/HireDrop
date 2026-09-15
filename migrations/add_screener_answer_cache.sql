-- Migration: cache the answers we generate for employer screener questions.
--
-- Why: /tools/answer-question was a straight pass-through to Sonnet — every
-- occurrence of "How many years of experience do you have with Excel?" was a
-- fresh paid call, on every job, forever. Screener questions are near-identical
-- across ATS forms, so the same user answers the same question dozens of times.
--
-- It is not only cost. Today the same question can get DIFFERENT answers on two
-- applications from the same person, because the model regenerates each time.
-- A cache makes one candidate's answers consistent, which is what a human doing
-- these forms by hand would produce.
--
-- Run in Supabase SQL editor: Dashboard → SQL Editor → New query → paste → Run

CREATE TABLE IF NOT EXISTS public.screener_answer_cache (
  user_id      UUID NOT NULL,
  -- sha256 over: normalised question + the option set + a profile fingerprint.
  -- The profile fingerprint is what expires the row when the candidate edits
  -- their resume or details — a stale "5+ years" answer is worse than a new call.
  cache_key    TEXT NOT NULL,
  -- Kept human-readable on purpose: when an answer looks wrong in the wild, the
  -- hash alone tells you nothing about which question produced it.
  question     TEXT NOT NULL,
  answer       TEXT NOT NULL,
  hits         INTEGER NOT NULL DEFAULT 0,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, cache_key)
);

-- For the eventual sweep of rows nobody has touched in months.
CREATE INDEX IF NOT EXISTS screener_answer_cache_last_used
  ON public.screener_answer_cache (last_used_at);

-- Backend uses the service role; no anon access needed. RLS on anyway, because
-- service_role bypasses it and anything else must not read another user's answers.
ALTER TABLE public.screener_answer_cache ENABLE ROW LEVEL SECURITY;

-- Hit counter as one statement, so measuring the cache can never race with itself
-- or cost a read-modify-write round trip on the answering path.
CREATE OR REPLACE FUNCTION public.bump_screener_cache_hit(p_user_id UUID, p_cache_key TEXT)
RETURNS void
LANGUAGE sql
AS $$
  UPDATE public.screener_answer_cache
     SET hits = hits + 1, last_used_at = now()
   WHERE user_id = p_user_id AND cache_key = p_cache_key;
$$;
