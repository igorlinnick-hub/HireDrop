-- Applications history must SURVIVE job-row deletion (counter integrity, GLOBAL_PLAN P3).
--
-- Problem: applications.job_id references jobs(id) ON DELETE CASCADE (legacy/v2.sql) —
-- deleting a job silently deletes the user's application history with it, and
-- /applications/history + the "Total Applied" stat undercount forever after any
-- future pool cleanup. History rows must be self-contained.
--
-- Fix, two parts:
--   1. Snapshot the display fields onto applications at save time (new columns below;
--      the API writes them from 2026-07-29 — see app/routers/applications.py).
--   2. Re-point the FK to ON DELETE SET NULL so a job deletion never cascades into
--      history. job_id stays useful as a live join key while the job exists.
--
-- Backfill copies current jobs data into the snapshots for existing rows.
-- Idempotent: safe to re-run.

alter table public.applications
  add column if not exists job_title text not null default '',
  add column if not exists company   text not null default '',
  add column if not exists platform  text not null default '',
  add column if not exists job_url   text not null default '';

-- Backfill snapshots from the jobs rows that still exist.
update public.applications a
set job_title = coalesce(nullif(a.job_title, ''), j.title, ''),
    company   = coalesce(nullif(a.company,   ''), j.company, ''),
    platform  = coalesce(nullif(a.platform,  ''), j.platform, ''),
    job_url   = coalesce(nullif(a.job_url,   ''), j.link, '')
from public.jobs j
where a.job_id = j.id
  and (a.job_title = '' or a.company = '' or a.platform = '' or a.job_url = '');

-- Decouple: job deletion must not delete history. job_id becomes nullable and the
-- cascade turns into SET NULL (the snapshot columns carry the display data).
alter table public.applications
  alter column job_id drop not null;

do $$
declare
  fk_name text;
begin
  select conname into fk_name
  from pg_constraint
  where conrelid = 'public.applications'::regclass
    and contype = 'f'
    and confrelid = 'public.jobs'::regclass;
  if fk_name is not null then
    execute format('alter table public.applications drop constraint %I', fk_name);
  end if;
  alter table public.applications
    add constraint applications_job_id_fkey
    foreign key (job_id) references public.jobs(id) on delete set null;
end $$;
