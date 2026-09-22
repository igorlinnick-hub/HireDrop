-- Referral link opens — the missing first step of the affiliate funnel.
--
-- Until now the funnel started at `signups`: someone scanned a card, landed,
-- left, and nothing recorded that they were ever here. That made the printed
-- material unmeasurable — `?ref=card` vs `?ref=stka` vs `?ref=stkb` exist
-- precisely to compare which artefact gets scanned, and there was nothing to
-- compare.
--
-- Deliberate choices:
--   * `code` is TEXT with no foreign key. Print codes (card, stka, stkb) are
--     not affiliates and never will be, and they are exactly what we want to
--     count. An FK here would silently drop the only rows the print A/B needs.
--   * One row per (code, visitor, day). A refresh, a back button, or a second
--     look at the page the same afternoon is the same person deciding — not
--     two clicks. Over-counting a partner's traffic is how an affiliate
--     dashboard starts lying in the partner's favour.
--   * The visitor is a salted hash of IP + user agent, never the IP itself.
--     We only ever ask "same person again?", so the raw address buys nothing.

create table if not exists affiliate_clicks (
  id            uuid primary key default gen_random_uuid(),
  code          text not null
                check (code = lower(code) and code ~ '^[a-z0-9][a-z0-9._-]{1,38}$'),
  day           date not null default (now() at time zone 'utc')::date,
  -- sha256(salt || ip || user-agent). Pseudonymous by construction.
  visitor_hash  text not null,
  landing_page  text,
  -- ?src= on the link, when the artefact carries one. Lets one code still say
  -- which door it came through without minting a second code.
  source        text,
  created_at    timestamptz not null default now()
);

create unique index if not exists affiliate_clicks_unique_day_idx
  on affiliate_clicks (code, day, visitor_hash);

create index if not exists affiliate_clicks_code_day_idx
  on affiliate_clicks (code, day desc);

comment on table affiliate_clicks is
  'One row per visitor per referral code per day. Counts link opens, including print codes that are not affiliates.';

-- Service_role only: written by POST /affiliate/click, read through the two
-- SECURITY DEFINER functions below. No client ever selects from it — a partner
-- reading raw rows could time-correlate visitors with signups.
alter table affiliate_clicks enable row level security;

-- ------------------------------------------------- what the admin board reads
-- Aggregated in SQL on purpose. PostgREST caps any select at 1000 rows without
-- saying so, and click rows are the one table here that will pass 1000 — an
-- admin board that counted them client-side would quietly plateau.
create or replace function affiliate_click_totals(p_from timestamptz, p_to timestamptz)
returns table (code text, clicks_total bigint, clicks_period bigint)
language sql
stable
security definer
set search_path = public
as $$
  select
    c.code,
    count(*)::bigint,
    count(*) filter (where c.created_at between p_from and p_to)::bigint
  from affiliate_clicks c
  group by c.code
$$;

revoke all on function affiliate_click_totals(timestamptz, timestamptz) from public;

-- --------------------------------------------- what the affiliate sees (+clicks)
-- Return type changes, so the old function has to go first: Postgres will not
-- CREATE OR REPLACE a function into a different signature.
drop function if exists affiliate_stats();

create or replace function affiliate_stats()
returns table (
  code           text,
  status         text,
  commission_pct numeric,
  paypal_email   text,
  clicks         integer,
  signups        integer,
  paying         integer,
  earned_cents   bigint,
  pending_cents  bigint,
  paid_cents     bigint
)
language sql
stable
security definer
set search_path = public
as $$
  select
    a.code,
    a.status,
    a.commission_pct,
    a.paypal_email,
    (select count(*) from affiliate_clicks k where k.code = a.code)::int,
    (select count(*) from referrals r where r.affiliate_id = a.id)::int,
    (select count(*) from referrals r
      where r.affiliate_id = a.id and r.first_paid_at is not null)::int,
    coalesce((select sum(c.amount_cents) from commissions c
      where c.affiliate_id = a.id and c.status <> 'reversed'), 0),
    coalesce((select sum(c.amount_cents) from commissions c
      where c.affiliate_id = a.id and c.status = 'accrued'), 0),
    coalesce((select sum(c.amount_cents) from commissions c
      where c.affiliate_id = a.id and c.status = 'paid_out'), 0)
  from affiliates a
  where a.user_id = auth.uid();
$$;

revoke all on function affiliate_stats() from public;
grant execute on function affiliate_stats() to authenticated;

notify pgrst, 'reload schema';
