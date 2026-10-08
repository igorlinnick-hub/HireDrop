-- The one-time check before the first run (Igor, 2026-10-07): the person looks at
-- everything HireDrop tells employers on one page and confirms it. Stamped once by
-- POST /profile/review; NULL = never confirmed (modules/review_sheet.py).
alter table public.profiles add column if not exists answers_confirmed_at timestamptz;
