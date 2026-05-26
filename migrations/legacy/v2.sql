-- HireDrop Phase 2 — Supabase Postgres schema
-- Run this in: Supabase Dashboard → SQL Editor
-- Run AFTER supabase-schema.sql (profiles table already exists)

-- ============================================================
-- JOBS TABLE
-- ============================================================
create table if not exists public.jobs (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid references auth.users(id) on delete cascade not null,
  title        text not null,
  company      text not null,
  link         text not null,
  status       text default 'new',
  date_found   timestamptz default now(),
  platform     text default 'remoteok',
  description  text default '',
  location     text default '',
  job_type     text default '',
  created_at   timestamptz default now()
);

alter table public.jobs enable row level security;

create policy "users_own_jobs"
  on public.jobs for all
  using (auth.uid() = user_id);

-- Unique constraint: same link per user (no duplicates)
create unique index if not exists jobs_user_link_idx on public.jobs (user_id, link);

-- ============================================================
-- APPLICATIONS TABLE
-- ============================================================
create table if not exists public.applications (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid references auth.users(id) on delete cascade not null,
  job_id       uuid references public.jobs(id) on delete cascade not null,
  date_applied timestamptz default now(),
  status       text default 'applied',
  cover_letter text default ''
);

alter table public.applications enable row level security;

create policy "users_own_applications"
  on public.applications for all
  using (auth.uid() = user_id);

-- ============================================================
-- CAMPAIGN STATES TABLE
-- Replaces campaign_state.json file on Railway
-- ============================================================
create table if not exists public.campaign_states (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid references auth.users(id) on delete cascade not null unique,
  running     boolean default false,
  filters     jsonb default '{}',
  started_at  timestamptz,
  updated_at  timestamptz default now()
);

alter table public.campaign_states enable row level security;

create policy "users_own_campaign_state"
  on public.campaign_states for all
  using (auth.uid() = user_id);

-- Auto-update updated_at on campaign_states
create trigger on_campaign_updated
  before update on public.campaign_states
  for each row execute function public.handle_updated_at();

-- ============================================================
-- HELPER VIEW: today's application stats
-- ============================================================
create or replace view public.today_stats as
select
  user_id,
  count(*) as applications_today
from public.applications
where date_applied >= current_date
group by user_id;

-- Allow users to see their own today_stats
create policy "users_see_own_today_stats"
  on public.today_stats for select
  using (auth.uid() = user_id);
