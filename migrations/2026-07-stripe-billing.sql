-- Stripe billing: link a profile to its Stripe customer + subscription so
-- webhook events (invoice.paid, subscription.updated/deleted) can be resolved
-- back to the owning user. Tier itself continues to live in the existing
-- subscription_tier / subscription_expires_at columns (shared with promo codes)
-- so get_tier() needs no change.

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS stripe_customer_id     text,
  ADD COLUMN IF NOT EXISTS stripe_subscription_id text;

-- Fast lookup by customer id in the webhook path (one row per customer).
CREATE UNIQUE INDEX IF NOT EXISTS profiles_stripe_customer_id_idx
  ON public.profiles (stripe_customer_id)
  WHERE stripe_customer_id IS NOT NULL;

-- No RLS policy change needed: these columns are written only by the backend
-- (service_role) from the webhook; clients never read or set them.
