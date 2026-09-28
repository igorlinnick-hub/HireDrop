-- Ad spend per (platform, day, ad) — the money side of the admin board's Ads tab.
-- Idempotent: safe to run twice.
--
-- Writers (all service_role, all upserts on the unique key below):
--   * platform='meta'   app/ads/meta_spend.py — Meta Insights, level=ad, daily
--   * platform='google' POST /api/v1/admin/ads/spend ← scripts/google_ads_spend.js
--   * platform='manual' scripts/ads_spend.py add — any other channel; account_id
--                       holds the utm_source that spend bought (e.g. reddit)
-- Reader: GET /api/v1/admin/metrics, section `ads`.
--
-- ad_id is the join key to signups: our ad URLs carry utm_content = the ad id
-- (Meta {{ad.id}}, Google {creative}). It is NOT NULL because a unique key with
-- NULLs never conflicts — a manual row without an ad would duplicate on re-add;
-- writers synthesise one (e.g. 'manual:<campaign>') instead.

create table if not exists public.ad_spend (
  id            bigserial primary key,
  date          date not null,
  platform      text not null check (platform in ('meta', 'google', 'manual')),
  account_id    text,
  campaign_id   text,
  campaign_name text,
  adset_id      text,
  ad_id         text not null,
  ad_name       text,
  spend_usd     numeric(12, 2) not null default 0,
  impressions   integer,
  clicks        integer,
  source        text,
  synced_at     timestamptz not null default now(),
  constraint ad_spend_platform_date_ad_key unique (platform, date, ad_id)
);

create index if not exists ad_spend_date_idx on public.ad_spend (date);

comment on table public.ad_spend is
  'Daily ad spend per ad (meta/google/manual). Service-role only; read by the admin board Ads tab. Unique (platform, date, ad_id) = upsert key.';

-- RLS on, NO policies: anon/authenticated see nothing; only service_role (which
-- bypasses RLS) reads or writes. Spend is business data, never user-facing.
alter table public.ad_spend enable row level security;
