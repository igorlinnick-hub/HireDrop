-- One row per search-results page the batch judge decided: which search phrase brought it,
-- how many postings got a verdict, how many cleared the person's bar. A phrase that keeps
-- bringing nothing that fits ("project manager" in San Diego is mostly construction for a
-- marketer) is then visible: the walk can skip it for the rest of a run and the dashboard
-- can ask the person to refine it. app/db/keyword_yield.py is the only writer and reader.
--
-- RLS on and NO policies: only the backend (service role) reads or writes it.
create table if not exists keyword_yield (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  user_id uuid not null references auth.users(id) on delete cascade,
  -- lowercased, trimmed: the same key the extension's keyword ledger uses
  keyword text not null,
  platform text not null,
  -- resume + apply mode + location fingerprint: a new resume starts every phrase over
  yield_version text not null,
  judged integer not null default 0,
  fits integer not null default 0
);

create index if not exists keyword_yield_user_created_idx on keyword_yield (user_id, created_at);

alter table keyword_yield enable row level security;
