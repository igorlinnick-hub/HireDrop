-- Stripe Connect payouts. Builds on migrations/add_affiliates.sql, which pays
-- partners by hand (scripts/affiliate_admin.py payout). This adds the pieces
-- an affiliate connects themselves, and the ones a daily job needs to pay them
-- automatically instead:
--
--   affiliates.stripe_account_id / payouts_enabled  — set by the backend only:
--     stripe_account_id from POST /affiliate/payouts/connect (the caller's OWN
--     row, filtered by user_id), payouts_enabled from the `account.updated`
--     webhook (app/routers/billing.py), keyed off the EVENT's account id —
--     never a client-supplied one, so an affiliate cannot mark anyone's
--     account, including their own, payable from the browser.
--
--   payouts.status / stripe_transfer_id — a payout row now has a lifecycle:
--     'pending' (claimed the commissions, Stripe call not confirmed yet) ->
--     'completed' (stripe_transfer_id set) or 'failed' (claimed nothing, see
--     scripts/run_affiliate_payouts.py). Existing manually-recorded payouts
--     default to 'completed' — that money had already left when the row was
--     written by hand.

alter table affiliates
  add column if not exists stripe_account_id text unique,
  add column if not exists payouts_enabled boolean not null default false;

alter table payouts
  add column if not exists stripe_transfer_id text unique,
  add column if not exists status text not null default 'completed'
    check (status in ('pending', 'completed', 'failed'));

-- affiliate_stats() return shape changes (adds connect_status), which Postgres
-- won't let CREATE OR REPLACE do across a signature change.
drop function if exists affiliate_stats();

create function affiliate_stats()
returns table (
  code           text,
  status         text,
  commission_pct numeric,
  paypal_email   text,
  clicks         integer,
  connect_status text,   -- 'none' | 'pending' | 'enabled'
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
    -- Carried over from migrations/add_affiliate_clicks.sql, which this
    -- DROP FUNCTION + CREATE also undoes if omitted — caught by review
    -- (2026-10-01) after it had already regressed the live function.
    (select count(*) from affiliate_clicks k where k.code = a.code)::int,
    case
      when a.stripe_account_id is null then 'none'
      when a.payouts_enabled then 'enabled'
      else 'pending'
    end,
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
