-- Promo codes: default grant is the tier we actually sell.
--
-- The table shipped with `grants_tier text not null default 'elite'`. `elite` was a legacy
-- 200/day tier that was never sold, so a promo code created without an explicit tier handed
-- out ~2.5x the paid product for free (the real ceiling is MAX_PER_PLATFORM 15 x 5 = 75/day
-- against a paying user's 30/day). The app already collapses premium/elite -> pro in
-- get_tier(), so nobody is over-served today — but the DB default kept minting rows that
-- only that collapse makes safe. Remove the trap at the source.

alter table public.promo_codes
  alter column grants_tier set default 'pro';

update public.promo_codes
   set grants_tier = 'pro'
 where grants_tier in ('elite', 'premium');

-- Keep the column honest going forward: 'pro' is the only tier checkout can grant.
alter table public.promo_codes
  drop constraint if exists promo_codes_grants_tier_check;

alter table public.promo_codes
  add constraint promo_codes_grants_tier_check check (grants_tier in ('pro'));

comment on column public.promo_codes.grants_tier is
  'Tier granted by this code. Only ''pro'' exists as a product (see app/billing_config.py).';
