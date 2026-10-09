-- One row per Anthropic call: who it was for, what it was for, which model, what it cost.
-- The console bills one number per day; deciding which feature eats it, whether a change
-- made it cheaper, or whether an account costs more than it pays needs it per call.
-- modules/ai_meter.py is the only writer; scripts/ai_cost_report.py and the admin AI-cost
-- section read the ai_calls_daily view below.
--
-- RLS on and NO policies: only the backend (service role) reads or writes it.
create table if not exists ai_calls (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  -- null when no user was bound to the call (ai_meter.attributed); the report shows that share
  user_id uuid references auth.users(id) on delete cascade,
  purpose text not null,
  model text not null,
  input_tokens integer not null default 0,
  output_tokens integer not null default 0,
  cache_read_tokens integer not null default 0,
  cache_write_tokens integer not null default 0,
  -- null when the model is missing from ai_meter.PRICES; the report counts those calls
  cost_usd numeric(12, 6)
);

create index if not exists ai_calls_created_at_idx on ai_calls (created_at);
create index if not exists ai_calls_user_created_idx on ai_calls (user_id, created_at);

alter table ai_calls enable row level security;

-- One row per UTC day x account x purpose x model. A month of calls is tens of thousands of
-- rows; the readers need a few hundred sums.
-- security_invoker: the view runs with the caller's rights, so the table's RLS (no policies)
-- keeps it as closed to anon/authenticated as the table itself.
create or replace view ai_calls_daily with (security_invoker = true) as
select
  (created_at at time zone 'utc')::date as day,
  user_id,
  purpose,
  model,
  count(*) as calls,
  coalesce(sum(cost_usd), 0) as cost_usd,
  count(*) filter (where cost_usd is null) as unpriced_calls,
  sum(input_tokens) as input_tokens,
  sum(output_tokens) as output_tokens,
  sum(cache_read_tokens) as cache_read_tokens,
  sum(cache_write_tokens) as cache_write_tokens
from ai_calls
group by 1, 2, 3, 4;

revoke all on ai_calls from anon, authenticated;
revoke all on ai_calls_daily from anon, authenticated;

notify pgrst, 'reload schema';
