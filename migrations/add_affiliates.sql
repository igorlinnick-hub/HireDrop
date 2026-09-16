-- Affiliate program — ledger tables + the two automations that keep them honest.
--
-- Shape of the program (content-lab/campus/AFFILIATE_PROPOSAL.md):
--   codes are issued BY HAND (scripts/affiliate_admin.py), commission accrues
--   from MONEY, not from signups, and payouts are done manually once a month.
--
-- Two things happen without anyone touching them:
--   1. profiles.attribution.ref -> referrals   (trigger below, first touch wins)
--   2. Stripe invoice.paid      -> commissions (app/routers/billing.py)
--
-- The commission RATE lives per-affiliate (commission_pct), not in code: changing
-- the program later must not silently rewrite what past partners were promised.

-- ---------------------------------------------------------------- affiliates
create table if not exists affiliates (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null unique references auth.users(id) on delete cascade,
  code           text not null unique
                 check (code = lower(code) and code ~ '^[a-z0-9][a-z0-9._-]{1,38}$'),
  paypal_email   text,
  commission_pct numeric(5,2) not null default 30.00 check (commission_pct between 0 and 100),
  status         text not null default 'active' check (status in ('pending','active','banned')),
  note           text,
  created_at     timestamptz not null default now()
);

comment on column affiliates.commission_pct is
  'Rate promised to THIS affiliate. Program-wide changes apply to new rows only.';

-- ----------------------------------------------------------------- referrals
-- One row per referred user, ever. UNIQUE(referred_user_id) is the first-touch
-- rule at the database level — the same rule lib/attribution.ts applies in the
-- browser, enforced where it cannot be bypassed.
create table if not exists referrals (
  id               uuid primary key default gen_random_uuid(),
  affiliate_id     uuid not null references affiliates(id) on delete cascade,
  referred_user_id uuid not null unique references auth.users(id) on delete cascade,
  status           text not null default 'signed_up'
                   check (status in ('signed_up','paying','churned','refunded')),
  first_seen_at    timestamptz not null default now(),
  first_paid_at    timestamptz
);

create index if not exists referrals_affiliate_idx on referrals(affiliate_id);

-- Self-referral guard. Can't be a CHECK (it reads another table), so it's a
-- trigger: one person cannot be both sides of their own commission.
create or replace function referrals_reject_self()
returns trigger
language plpgsql
as $$
begin
  if exists (
    select 1 from affiliates a
    where a.id = new.affiliate_id and a.user_id = new.referred_user_id
  ) then
    raise exception 'self-referral rejected for affiliate %', new.affiliate_id;
  end if;
  return new;
end;
$$;

drop trigger if exists referrals_no_self on referrals;
create trigger referrals_no_self
  before insert or update on referrals
  for each row execute function referrals_reject_self();

-- ------------------------------------------------------------------- payouts
-- Written by hand after the money leaves PayPal. Declared before commissions
-- because commissions.payout_id points at it.
create table if not exists payouts (
  id           uuid primary key default gen_random_uuid(),
  affiliate_id uuid not null references affiliates(id) on delete cascade,
  amount_cents integer not null check (amount_cents > 0),
  method       text not null default 'paypal',
  external_ref text,
  note         text,
  paid_at      timestamptz not null default now()
);

create index if not exists payouts_affiliate_idx on payouts(affiliate_id);

-- --------------------------------------------------------------- commissions
-- One row per paid Stripe invoice. stripe_invoice_id UNIQUE is the whole
-- idempotency story: Stripe delivers at-least-once, and a re-delivered
-- invoice.paid must never accrue twice.
create table if not exists commissions (
  id                uuid primary key default gen_random_uuid(),
  affiliate_id      uuid not null references affiliates(id) on delete cascade,
  referral_id       uuid not null references referrals(id) on delete cascade,
  stripe_invoice_id text not null unique,
  gross_cents       integer not null,  -- what the customer actually paid
  amount_cents      integer not null,  -- our commission out of that
  commission_pct    numeric(5,2) not null,  -- rate at the time, frozen
  currency          text not null default 'usd',
  status            text not null default 'accrued'
                    check (status in ('accrued','reversed','paid_out')),
  payout_id         uuid references payouts(id) on delete set null,
  period_start      timestamptz,
  period_end        timestamptz,
  created_at        timestamptz not null default now()
);

create index if not exists commissions_affiliate_idx on commissions(affiliate_id, created_at desc);
create index if not exists commissions_status_idx on commissions(status);

-- ------------------------------------------ attribution -> referral (trigger)
-- profiles.attribution is written once at signup (first touch wins, 60-day
-- cookie). When its ref matches a live affiliate code, the referral row appears
-- here. Codes that aren't affiliates (?ref=card, ?ref=stka — the print
-- material) simply don't match and fall through.
create or replace function link_referral_from_attribution()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  ref_code text;
  aff      affiliates%rowtype;
begin
  ref_code := lower(nullif(new.attribution->>'ref', ''));
  if ref_code is null then
    return new;
  end if;

  select * into aff from affiliates where code = ref_code and status = 'active';
  if not found or aff.user_id = new.user_id then
    return new;  -- unknown code, or someone referring themselves
  end if;

  insert into referrals (affiliate_id, referred_user_id)
  values (aff.id, new.user_id)
  on conflict (referred_user_id) do nothing;  -- first touch wins

  return new;
end;
$$;

drop trigger if exists profiles_link_referral on profiles;
create trigger profiles_link_referral
  after insert or update of attribution on profiles
  for each row execute function link_referral_from_attribution();

-- Backfill: profiles that already carry a ref from before this migration.
insert into referrals (affiliate_id, referred_user_id, first_seen_at)
select a.id, p.user_id, coalesce((p.attribution->>'captured_at')::timestamptz, now())
from profiles p
join affiliates a
  on a.code = lower(p.attribution->>'ref')
 and a.status = 'active'
 and a.user_id <> p.user_id
on conflict (referred_user_id) do nothing;

-- ------------------------------------------------------- what the affiliate sees
-- One call, the four numbers on /dashboard/affiliate. SECURITY DEFINER because
-- it counts rows (referrals) the affiliate must never be able to read directly —
-- it returns totals, never who signed up. Scoped to auth.uid(): the caller can
-- only ever get their own row.
create or replace function affiliate_stats()
returns table (
  code           text,
  status         text,
  commission_pct numeric,
  paypal_email   text,
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

-- --------------------------------------------------------------------- RLS
-- service_role bypasses RLS, so every one of these tables is written only by
-- the backend. Affiliates get SELECT on their own affiliate row and their own
-- commission/payout ledger — and nothing on `referrals`, which would let them
-- correlate individual people to individual payments.
alter table affiliates  enable row level security;
alter table referrals   enable row level security;
alter table commissions enable row level security;
alter table payouts     enable row level security;

drop policy if exists affiliates_select_own on affiliates;
create policy affiliates_select_own on affiliates
  for select to authenticated
  using (user_id = auth.uid());

drop policy if exists commissions_select_own on commissions;
create policy commissions_select_own on commissions
  for select to authenticated
  using (exists (
    select 1 from affiliates a where a.id = commissions.affiliate_id and a.user_id = auth.uid()
  ));

drop policy if exists payouts_select_own on payouts;
create policy payouts_select_own on payouts
  for select to authenticated
  using (exists (
    select 1 from affiliates a where a.id = payouts.affiliate_id and a.user_id = auth.uid()
  ));

notify pgrst, 'reload schema';
