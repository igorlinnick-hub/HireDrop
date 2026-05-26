-- HireDrop Phase 3+4 — Supabase schema v3
-- Run this in: Supabase Dashboard → SQL Editor → New query → paste → Run.
-- Idempotent: safe to re-run, all CREATEs use IF NOT EXISTS.
--
-- Companion bucket setup (UI, not SQL):
--   Supabase Dashboard → Storage → "New bucket" → name: resumes →
--   Public: OFF → Save. Then run the bucket policies block below.

-- ============================================================
-- PR 3.4b — cover_letter_usage (per-user/per-day counter)
-- ============================================================
create table if not exists public.cover_letter_usage (
  user_id    uuid references auth.users(id) on delete cascade not null,
  date       date not null default current_date,
  count      integer not null default 0,
  updated_at timestamptz not null default now(),
  primary key (user_id, date)
);

alter table public.cover_letter_usage enable row level security;

drop policy if exists "users_own_usage" on public.cover_letter_usage;
create policy "users_own_usage"
  on public.cover_letter_usage for all
  using (auth.uid() = user_id);

create index if not exists cover_letter_usage_date_idx
  on public.cover_letter_usage (date desc);

-- ============================================================
-- PR 4.1 — platform_selectors (DOM selectors as data, not code)
-- ============================================================
create table if not exists public.platform_selectors (
  platform       text primary key,
  version        integer not null default 1,
  selectors_json jsonb not null,
  updated_at     timestamptz not null default now()
);

alter table public.platform_selectors enable row level security;

-- Read-only for any authenticated user (extension fetches via backend).
drop policy if exists "auth_read_selectors" on public.platform_selectors;
create policy "auth_read_selectors"
  on public.platform_selectors for select
  to authenticated
  using (true);

-- Writes only via service_role (backend admin endpoint, not user requests).

-- Seed initial Indeed selectors (mirrors current content.js hardcoded values).
insert into public.platform_selectors (platform, version, selectors_json) values (
  'indeed',
  1,
  jsonb_build_object(
    'jobCards', jsonb_build_array(
      '.job_seen_beacon',
      '.resultContent',
      '.jobsearch-ResultsList li',
      '[data-jk]',
      'div[class*="cardOutline"]',
      'td.resultContent'
    ),
    'applyButton', jsonb_build_array(
      'button[id*="indeedApply"]',
      '.ia-IndeedApplyButton',
      'button[class*="IndeedApply"]',
      'button[aria-label*="Apply now"]',
      'a[href*="/applystart"]',
      'button[data-testid*="apply"]'
    ),
    'fields', jsonb_build_object(
      'firstName', jsonb_build_array(
        'input[name*="firstName" i]',
        'input[id*="firstName" i]',
        'input[name*="first_name" i]',
        'input[autocomplete="given-name"]'
      ),
      'lastName', jsonb_build_array(
        'input[name*="lastName" i]',
        'input[id*="lastName" i]',
        'input[name*="last_name" i]',
        'input[autocomplete="family-name"]'
      ),
      'email', jsonb_build_array(
        'input[type="email"]',
        'input[name*="email" i]',
        'input[id*="email" i]',
        'input[autocomplete="email"]'
      ),
      'phone', jsonb_build_array(
        'input[type="tel"]',
        'input[name*="phone" i]',
        'input[id*="phone" i]',
        'input[autocomplete="tel"]'
      ),
      'coverLetter', jsonb_build_array(
        'textarea[name*="coverletter" i]',
        'textarea[name*="cover_letter" i]',
        'textarea[name*="message" i]',
        'textarea[aria-label*="cover letter" i]',
        'textarea[id*="coverLetter" i]',
        'textarea[id*="message" i]'
      )
    ),
    'submitVerify', jsonb_build_object(
      'urlHints', jsonb_build_array('/applied', 'postapply', 'post_apply', 'thank-you', 'thankyou'),
      'textPhrases', jsonb_build_array(
        'application submitted',
        'thanks for applying',
        'successfully applied',
        'application sent',
        'you''ve applied',
        'you have applied',
        'we''ve received your application'
      )
    )
  )
)
on conflict (platform) do nothing;

-- ============================================================
-- PR 4.2 — activity_log (centralized observability)
-- ============================================================
create table if not exists public.activity_log (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid references auth.users(id) on delete cascade not null,
  trace_id      text,
  timestamp     timestamptz not null default now(),
  level         text not null default 'info',  -- info | warn | error
  phase         text,                          -- discovery | filter | apply | verify | ...
  message       text not null,
  metadata_json jsonb not null default '{}'::jsonb
);

alter table public.activity_log enable row level security;

drop policy if exists "users_own_activity" on public.activity_log;
create policy "users_own_activity"
  on public.activity_log for all
  using (auth.uid() = user_id);

create index if not exists activity_log_user_time_idx
  on public.activity_log (user_id, timestamp desc);

create index if not exists activity_log_trace_idx
  on public.activity_log (trace_id) where trace_id is not null;

-- ============================================================
-- PR 3.5 — Storage bucket policies for "resumes"
-- ============================================================
-- IMPORTANT: create the bucket via UI first (Storage → New bucket → name: resumes,
-- Public: OFF). Then run this block. RLS on storage.objects is enforced by Supabase.

drop policy if exists "users_own_resumes_select" on storage.objects;
create policy "users_own_resumes_select"
  on storage.objects for select
  to authenticated
  using (bucket_id = 'resumes' and (auth.uid())::text = (storage.foldername(name))[1]);

drop policy if exists "users_own_resumes_insert" on storage.objects;
create policy "users_own_resumes_insert"
  on storage.objects for insert
  to authenticated
  with check (bucket_id = 'resumes' and (auth.uid())::text = (storage.foldername(name))[1]);

drop policy if exists "users_own_resumes_update" on storage.objects;
create policy "users_own_resumes_update"
  on storage.objects for update
  to authenticated
  using (bucket_id = 'resumes' and (auth.uid())::text = (storage.foldername(name))[1]);

drop policy if exists "users_own_resumes_delete" on storage.objects;
create policy "users_own_resumes_delete"
  on storage.objects for delete
  to authenticated
  using (bucket_id = 'resumes' and (auth.uid())::text = (storage.foldername(name))[1]);

-- Convention: each user uploads to path `<user_id>/resume.pdf`. The first
-- folder segment must equal auth.uid() — that's how the policies above
-- isolate users. The backend will use this convention when generating
-- signed URLs (PR 3.5).

-- ============================================================
-- Done. Verification queries:
-- ============================================================
-- select count(*) from public.cover_letter_usage;       -- should be 0
-- select platform, version from public.platform_selectors;  -- should show 'indeed' v1
-- select count(*) from public.activity_log;             -- should be 0
-- select id, name, public from storage.buckets where id = 'resumes';
