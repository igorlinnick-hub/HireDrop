-- Let an affiliate's commission ledger outlive the account of the person they referred.
--
-- Before: referrals.referred_user_id is NOT NULL and cascades from auth.users, and
-- commissions.referral_id cascades from referrals. Deleting a referred user's account
-- (privacy-policy erasure, scripts/delete_account.py) would therefore delete the
-- AFFILIATE's commissions for that user — another person's money records.
--
-- After: the column is nullable and the FK sets it to NULL. scripts/delete_account.py
-- anonymizes such a referral explicitly (referred_user_id = NULL) before deleting the
-- auth user; the FK change is the backstop for anything that deletes a user another way.
-- Readers only count referrals per affiliate (affiliate_* SECURITY DEFINER functions),
-- and NULLs do not collide under the UNIQUE(referred_user_id) first-touch rule.
--
-- The FK is found by what it is (the one foreign key on referred_user_id), not by its
-- assumed default name: `drop constraint if exists <guessed name>` would silently drop
-- nothing, add a second FK, and leave the cascading one in place. Anything other than
-- exactly one FK on the column aborts the whole transaction.
--
-- Applied to prod 2026-10-06 (FK verified: ON DELETE SET NULL, column nullable). The
-- `notify` at the end makes PostgREST drop the column from the OpenAPI `required` list
-- that scripts/delete_account.py reads to decide anonymize-vs-refuse.

begin;

alter table public.referrals alter column referred_user_id drop not null;

do $$
declare
  fks text[];
begin
  select array_agg(c.conname order by c.conname) into fks
  from pg_constraint c
  where c.conrelid = 'public.referrals'::regclass
    and c.contype = 'f'
    and c.conkey = array[(
      select a.attnum from pg_attribute a
      where a.attrelid = 'public.referrals'::regclass and a.attname = 'referred_user_id'
    )]::int2[];

  if coalesce(array_length(fks, 1), 0) <> 1 then
    raise exception 'referrals.referred_user_id: expected exactly one FK, found %', fks;
  end if;

  execute format('alter table public.referrals drop constraint %I', fks[1]);
end $$;

alter table public.referrals
  add constraint referrals_referred_user_id_fkey
  foreign key (referred_user_id) references auth.users(id) on delete set null;

commit;

notify pgrst, 'reload schema';
