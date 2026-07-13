-- Stripe webhook idempotency. Stripe delivers events AT LEAST ONCE (and can send
-- them out of order), so we record every processed event id and skip duplicates —
-- otherwise a re-delivered event could double-grant / corrupt tier state.

CREATE TABLE IF NOT EXISTS public.stripe_events (
  event_id     text PRIMARY KEY,
  type         text,
  processed_at timestamptz NOT NULL DEFAULT now()
);

-- Service_role only (written by the backend webhook); clients never read/write it.
ALTER TABLE public.stripe_events ENABLE ROW LEVEL SECURITY;
