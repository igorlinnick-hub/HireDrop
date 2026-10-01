-- Migration: store the fit judge's verdict on the pool row (prejudged queue, 09-30)
-- Apply: supabase db query --linked -f migrations/add_job_fit_verdict.sql
--        then: notify pgrst, 'reload schema';
--
-- Until now the verdict lived only in the extension's memory, judged one posting at a
-- time during the run. modules/fit_queue.py judges ahead of the run and keeps the result
-- here; fit_version is ai_fit_judge.verdict_version() — the resume/preferences/bar the
-- verdict was reached against — so an edited resume sends the row back to the judge
-- instead of showing a stale "why this fits you".

ALTER TABLE public.jobs
  ADD COLUMN IF NOT EXISTS fit_score SMALLINT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS fit_reason TEXT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS fit_model TEXT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS fit_version TEXT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS fit_judged_at TIMESTAMPTZ DEFAULT NULL;

COMMENT ON COLUMN public.jobs.fit_score IS 'Fit judge score 0-100 (modules/ai_fit_judge.py); NULL = not judged yet';
COMMENT ON COLUMN public.jobs.fit_reason IS 'Judge''s one-line reason — the "why this fits you" line';
COMMENT ON COLUMN public.jobs.fit_model IS 'Model that produced the verdict (cascade telemetry)';
COMMENT ON COLUMN public.jobs.fit_version IS 'verdict_version() at judging time; mismatch = re-judge';
COMMENT ON COLUMN public.jobs.fit_judged_at IS 'When the verdict was stored';
