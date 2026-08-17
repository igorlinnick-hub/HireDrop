-- Mailing address for job applications.
--
-- ZipRecruiter's contact step asks for street / city / state / postal. It LABELS them
-- Optional and then refuses to advance while they are blank (live 2026-08-15: the
-- hand-back ledger named exactly ['address','state','postal'] on the jobs that stalled).
-- We hold only the search location ("Miami, Florida, US"), which is a search preference,
-- not an address — and an application must never carry invented applicant data, so the
-- user supplies these once and every platform filler reads them.
--
-- Apply in the Supabase SQL editor.
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS street_address text DEFAULT '',
  ADD COLUMN IF NOT EXISTS city           text DEFAULT '',
  ADD COLUMN IF NOT EXISTS state          text DEFAULT '',
  ADD COLUMN IF NOT EXISTS postal_code    text DEFAULT '';
