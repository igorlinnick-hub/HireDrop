-- Current employment for job applications.
--
-- "Current company / current employer / current job title" is the single biggest
-- hand-back cause on ATS forms: 12 of the 21 required questions we'd leave BLANK on
-- the 320-form Greenhouse measure (scripts/form_coverage.py, 2026-09-04) are exactly
-- these, plus variants like "who is your current or previous employer?" that today
-- burn an LLM call each. The profile simply had nowhere to hold them.
--
-- An application must never carry invented applicant data — the user supplies these
-- once in Settings and every platform filler reads them; blank means hand back.
--
-- Apply in the Supabase SQL editor.
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS current_employer text DEFAULT '',
  ADD COLUMN IF NOT EXISTS current_title    text DEFAULT '';
