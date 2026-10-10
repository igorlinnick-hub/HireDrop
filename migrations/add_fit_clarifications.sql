-- Drop the clarifier: once or twice a day Drop asks the person about ONE posting the fit judge
-- was unsure of ("would this suit you? 0 to 10, or 👍 / 👎"). One row per question offered;
-- the answer lands on the same row. modules/fit_clarify.py decides what to ask,
-- app/db/fit_clarify.py is the only writer and reader.
--
-- Two readers of the answers: the person's own list (step 3: "don't show jobs like this",
-- "apply to this one") and the measure of how often the judge is right in its disputed zone
-- (scripts/clarify_report.py, for the ai-economics lane). The posting is snapshotted here
-- (title, company, score, bar, model) because pool rows are recycled and the measure must
-- outlive them — so job_id carries no foreign key.
--
-- RLS on, NO policies, no grants: only the backend (service role) reads or writes it.
create table if not exists fit_clarifications (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  user_id uuid not null references auth.users(id) on delete cascade,
  job_id uuid,
  -- title family (modules/fit_clarify.category): one family is never asked twice
  category text not null,
  label text not null,
  title text,
  company text,
  location text,
  platform text,
  link text,
  -- the verdict the question is about, as it stood when asked
  fit_score smallint not null,
  bar smallint not null,
  side text not null check (side in ('below', 'above')),
  fit_model text,
  fit_version text,
  -- the person opened the chat with this question in it
  seen_at timestamptz,
  answered_at timestamptz,
  rating smallint check (rating between 0 and 10),
  thumb text check (thumb in ('up', 'down')),
  skipped boolean not null default false,
  -- the person's own words, for auditing what Drop made of them
  answer_text text,
  -- what the answer led to (step 3): hidden | applied
  outcome text
);

create index if not exists fit_clarifications_user_created_idx
  on fit_clarifications (user_id, created_at);

alter table fit_clarifications enable row level security;
revoke all on fit_clarifications from anon, authenticated;
