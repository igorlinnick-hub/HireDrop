-- Fixes a bug caught before merge: payouts.paid_at was `not null default now()`
-- (correct for the OLD manual-PayPal flow, where the row is only ever written
-- AFTER the money already left — scripts/affiliate_admin.py sets it implicitly
-- via this default). scripts/run_affiliate_payouts.py now inserts a `pending`
-- row to CLAIM commissions before Stripe confirms anything, and that insert
-- also omits paid_at — so the same default would stamp "paid" at the moment
-- of the CLAIM, not the actual transfer. A payout stuck `pending` after a
-- failed Stripe call would then show up as already paid in
-- app/routers/admin.py's `paid_period` (summed by paid_at falling in range)
-- and in the website's payout history.
--
-- Fix: paid_at becomes nullable with no default. Each writer sets it
-- explicitly at the moment that is actually true for it:
--   scripts/affiliate_admin.py      -> now(), at insert (money already sent)
--   scripts/run_affiliate_payouts.py -> left null while pending, set on the
--                                       completion update once Stripe confirms

alter table payouts alter column paid_at drop not null;
alter table payouts alter column paid_at drop default;

notify pgrst, 'reload schema';
