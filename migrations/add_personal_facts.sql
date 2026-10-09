-- What the person told us about their own circumstances, asked once and reused
-- (modules/personal_facts.py): "willing to relocate to Miami? No", "Moving to San Diego
-- in December" (mentioned in cover letters where it fits), "work weekends? Yes".
--
-- Until now an answer the person gave to a handed-back form lived only on that job's
-- `handbacks.answers` row, so the next employer asking the same thing handed the form back
-- again. A jsonb list on the profile: small (≤ 40), always read whole, already loaded on
-- every answer and letter.
--
-- Safe before or after the deploy: the code reads a missing column as "no facts yet"
-- (app/db/personal_facts.py) and only saving needs it.
alter table profiles add column if not exists personal_facts jsonb not null default '[]'::jsonb;

notify pgrst, 'reload schema';
