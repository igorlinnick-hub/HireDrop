-- Promo codes — grant a paid tier for free to trusted people.
--
-- SECURITY MODEL (this is the whole point):
--   * RLS is ENABLED with NO policies on both tables → the anon/authenticated
--     Supabase clients get ZERO access. Codes can NOT be listed, read, or
--     guessed from the browser (unlike the older `invite_codes`, whose SELECT
--     policy let any client dump every active code). Only the backend, using
--     the service_role key, can touch these tables.
--   * The tier upgrade happens server-side only. A user can never set their own
--     subscription_tier — they submit a code, the backend validates it here.
--   * Codes are high-entropy random strings (see redeem flow) → not guessable.
--   * max_uses + code_expires_at + active give blast-radius control + kill switch.
--   * promo_redemptions is an audit log + a unique(user_id, code) guard against
--     a single user redeeming the same code twice.

create table if not exists public.promo_codes (
  id              uuid primary key default gen_random_uuid(),
  code            text not null unique,
  grants_tier     text not null default 'elite',   -- 'pro' | 'elite'
  duration_days   integer,                          -- null = never expires for the user
  max_uses        integer,                          -- null = unlimited redemptions
  uses            integer not null default 0,
  active          boolean not null default true,    -- kill switch
  code_expires_at timestamptz,                       -- null = the code never stops working
  notes           text,                              -- who it's for / campaign label
  created_at      timestamptz not null default now()
);

create table if not exists public.promo_redemptions (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null,
  code         text not null,
  tier_granted text,
  expires_at   timestamptz,
  redeemed_at  timestamptz not null default now(),
  unique (user_id, code)
);

create index if not exists promo_redemptions_user_idx on public.promo_redemptions (user_id);

-- RLS on, no policies → clients denied entirely; service_role bypasses RLS.
alter table public.promo_codes enable row level security;
alter table public.promo_redemptions enable row level security;

-- Atomic use-claim: increments `uses` only if the code is active, not past its
-- window, and under max_uses — all in one statement so concurrent redemptions
-- can't over-spend a limited code. Returns true on a successful claim.
create or replace function public.claim_promo_use(p_code text)
returns boolean as $$
declare claimed boolean;
begin
  update public.promo_codes
     set uses = uses + 1
   where code = p_code
     and active = true
     and (code_expires_at is null or code_expires_at > now())
     and (max_uses is null or uses < max_uses)
  returning true into claimed;
  return coalesce(claimed, false);
end;
$$ language plpgsql security definer;

-- Lock the SECURITY DEFINER function to the backend (service_role) only. Without
-- this, PostgREST would let anon/authenticated call it directly and burn a code's
-- `uses` without redeeming (griefing a limited code).
revoke execute on function public.claim_promo_use(text) from public, anon, authenticated;

-- ── Create a code (run manually as the founder; only you have DB access) ──
-- Generate a high-entropy code, e.g. in a shell:
--   python3 -c "import secrets;print('HD-'+secrets.token_urlsafe(12))"
-- then:
--   insert into public.promo_codes (code, grants_tier, duration_days, max_uses, notes)
--   values ('HD-xxxxxxxxxxxxxxxx', 'elite', 90, 25, 'trusted early users');
