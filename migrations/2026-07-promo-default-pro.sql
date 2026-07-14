-- The Weekly/Monthly model has ONE paid tier ("pro"). Promo codes now grant "pro"
-- by default (was "elite"). get_tier() already maps any legacy premium/elite grant
-- to pro at read time, so existing codes/profiles keep working — this just aligns
-- the DB default for newly created codes.

ALTER TABLE public.promo_codes ALTER COLUMN grants_tier SET DEFAULT 'pro';
