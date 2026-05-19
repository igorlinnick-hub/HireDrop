-- =============================================================================
-- Cold-email outreach feature — 2026-05-18
-- =============================================================================
-- Tables: email_templates, contacts, campaigns, campaign_sends, unsubscribe_tokens
-- Profiles columns: gmail_refresh_token, gmail_email (for Gmail OAuth-send)
-- All tables RLS-protected: users only see their own rows.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Profiles: add Gmail OAuth columns
-- ---------------------------------------------------------------------------
alter table public.profiles
  add column if not exists gmail_refresh_token text,  -- TODO encrypt at rest (Supabase Vault) before public launch
  add column if not exists gmail_email text;


-- ---------------------------------------------------------------------------
-- email_templates — user-owned outreach templates
-- ---------------------------------------------------------------------------
create table if not exists public.email_templates (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid references auth.users(id) on delete cascade not null,
  name        text not null,
  subject     text not null,
  body_md     text not null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create index if not exists email_templates_user_idx on public.email_templates (user_id);

alter table public.email_templates enable row level security;
drop policy if exists "users_own_templates" on public.email_templates;
create policy "users_own_templates"
  on public.email_templates for all
  using (auth.uid() = user_id);


-- ---------------------------------------------------------------------------
-- contacts — user-owned recruiter/lead lists
-- ---------------------------------------------------------------------------
create table if not exists public.contacts (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid references auth.users(id) on delete cascade not null,
  email         text not null,
  name          text,
  company       text,
  role          text,
  source        text,                            -- 'csv' | 'manual' | future: 'apollo' etc.
  tags          text[] not null default '{}',
  unsubscribed  boolean not null default false,
  created_at    timestamptz not null default now()
);
-- (user_id, lowercased email) uniqueness — case-insensitive de-duplication
create unique index if not exists contacts_user_email_idx
  on public.contacts (user_id, lower(email));

alter table public.contacts enable row level security;
drop policy if exists "users_own_contacts" on public.contacts;
create policy "users_own_contacts"
  on public.contacts for all
  using (auth.uid() = user_id);


-- ---------------------------------------------------------------------------
-- campaigns — batch sends (template × contact list)
-- ---------------------------------------------------------------------------
create table if not exists public.campaigns (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid references auth.users(id) on delete cascade not null,
  template_id  uuid references public.email_templates(id) on delete set null,
  name         text not null,
  status       text not null default 'draft',  -- draft | running | paused | done | failed
  started_at   timestamptz,
  finished_at  timestamptz,
  created_at   timestamptz not null default now()
);
create index if not exists campaigns_user_idx on public.campaigns (user_id);

alter table public.campaigns enable row level security;
drop policy if exists "users_own_campaigns" on public.campaigns;
create policy "users_own_campaigns"
  on public.campaigns for all
  using (auth.uid() = user_id);


-- ---------------------------------------------------------------------------
-- campaign_sends — per-recipient row inside a campaign
-- ---------------------------------------------------------------------------
create table if not exists public.campaign_sends (
  id                uuid primary key default gen_random_uuid(),
  campaign_id       uuid references public.campaigns(id) on delete cascade not null,
  contact_id        uuid references public.contacts(id) on delete cascade not null,
  status            text not null default 'queued',  -- queued | sent | failed | skipped
  gmail_message_id  text,
  error             text,
  sent_at           timestamptz,
  opened_at         timestamptz,
  replied_at        timestamptz,
  created_at        timestamptz not null default now()
);
create index if not exists campaign_sends_campaign_idx on public.campaign_sends (campaign_id);
create index if not exists campaign_sends_contact_idx on public.campaign_sends (contact_id);

alter table public.campaign_sends enable row level security;
drop policy if exists "users_own_campaign_sends" on public.campaign_sends;
create policy "users_own_campaign_sends"
  on public.campaign_sends for all
  using (
    exists (
      select 1 from public.campaigns c
      where c.id = campaign_sends.campaign_id and c.user_id = auth.uid()
    )
  );


-- ---------------------------------------------------------------------------
-- unsubscribe_tokens — CAN-SPAM one-click unsubscribe support
-- ---------------------------------------------------------------------------
-- Each (campaign_send → recipient) gets a token. Hitting /u/{token} marks the
-- contact as unsubscribed and short-circuits future sends to that contact.
create table if not exists public.unsubscribe_tokens (
  token       text primary key,
  contact_id  uuid references public.contacts(id) on delete cascade not null,
  created_at  timestamptz not null default now()
);
create index if not exists unsubscribe_tokens_contact_idx on public.unsubscribe_tokens (contact_id);

-- No RLS on this table: the route resolving /u/{token} runs anonymously with the
-- service role on the backend. Token itself is the access control.


-- ---------------------------------------------------------------------------
-- Done. Verification:
--   select count(*) from public.email_templates;   -- 0
--   select count(*) from public.contacts;          -- 0
--   select count(*) from public.campaigns;         -- 0
--   select count(*) from public.campaign_sends;    -- 0
--   select count(*) from public.unsubscribe_tokens; -- 0
--   select column_name from information_schema.columns
--     where table_name = 'profiles' and column_name like 'gmail%';
--     -- → gmail_refresh_token, gmail_email
-- ---------------------------------------------------------------------------
