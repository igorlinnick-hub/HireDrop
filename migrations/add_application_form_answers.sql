-- The questions on the employer's form and the answers we gave, per application.
--
-- The extension snapshots the form right before each Continue/Submit click
-- (content.js collectFormAnswers) and sends it with /applications/save as
-- [{"q": "...", "a": "..."}]. History shows it to the person: what was said in their
-- name, before the interview. Contact fields and the letter are not in it.
--
-- Idempotent: safe to re-run.

alter table public.applications
  add column if not exists form_answers jsonb not null default '[]'::jsonb;
