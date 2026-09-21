-- Migration: how far the filler actually got before handing the job back.
--
-- The list shows this as progress ("5 of 6 screens done"), and progress is the whole
-- reason the user opens it at all: "почти дожато" and "надо заполнять заново" are
-- completely different decisions. So the number must be one we OBSERVED — the count of
-- form screens actually completed — never a guess dressed up as a percentage.
--
-- 0 = we never completed a screen (or the row predates this column). The UI treats 0 as
-- "no progress to show" and says nothing rather than drawing an empty bar.
--
-- Run: supabase db query --linked -f migrations/add_handback_steps.sql

ALTER TABLE public.handbacks
  ADD COLUMN IF NOT EXISTS steps_done INT NOT NULL DEFAULT 0;
