-- Country + "I don't have a LinkedIn" — the two answers the pre-Start form needs that the
-- profile had nowhere to hold.
--
-- Country was the single most frequent question a form stopped on (10 of 39 hand-backs,
-- 30 days to 2026-09-27) and the filler could only GUESS it from the phone prefix.
-- no_linkedin makes "none" an answer, so Start can require the LinkedIn question without
-- forcing people who have no profile to invent one. See modules/employer_answers.py.
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS country     text    DEFAULT '',
  ADD COLUMN IF NOT EXISTS no_linkedin boolean DEFAULT false;
