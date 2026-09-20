-- Affiliate applications — the front door to the program.
--
-- Until now codes were handed out in DMs (scripts/affiliate_admin.py). This
-- table is the inbox for people who find us instead: a QR on a campus card, a
-- link in a post. Anyone can apply; nobody is approved automatically.
--
-- Why a separate table and not a `pending` row in `affiliates`: that table's
-- user_id is NOT NULL and references auth.users, so it cannot hold someone who
-- has not signed up yet — and the whole point here is that a stranger can
-- apply. Approval is what turns an application into an affiliate row.
--
-- Two approval paths, both in app/routers/affiliate.py:
--   email already has an account -> affiliates row created immediately, active
--   no account yet               -> code stays reserved here, and the trigger
--                                   at the bottom materialises the affiliate
--                                   the moment that email signs up.

create table if not exists affiliate_applications (
  id              uuid primary key default gen_random_uuid(),
  -- Stored lowercase; the app normalises before insert. This is the join key
  -- to an account that may not exist yet.
  email           text not null check (email = lower(email) and position('@' in email) > 1),
  name            text not null check (length(trim(name)) between 1 and 120),
  -- The link they are asking for: hiredrop.io/?ref=<desired_code>. Same shape
  -- rule as affiliates.code, so an approval can never produce an invalid code.
  desired_code    text not null
                  check (desired_code = lower(desired_code)
                         and desired_code ~ '^[a-z0-9][a-z0-9._-]{1,38}$'),
  -- Quiz answers. Free text on purpose: this is read by a human deciding
  -- whether to trust someone, not aggregated into a chart.
  audience        text,           -- where they will share it (channel, handle, link)
  audience_size   text,           -- their own words: "3k on IG", "a class of 40"
  promo_plan      text,           -- how they intend to talk about it
  paypal_email    text,           -- where a payout would go
  source          text,           -- ?src=card / ?src=post — which door they came through
  -- Kept only to review abuse (one person filing twenty applications). Not
  -- shown on the board, not used for anything else.
  applicant_ip    text,
  status          text not null default 'new'
                  check (status in ('new', 'approved', 'rejected')),
  -- Admin's words: why rejected, or anything worth remembering about them.
  note            text,
  created_at      timestamptz not null default now(),
  reviewed_at     timestamptz,
  -- Set when the application became a real affiliate row.
  affiliate_id    uuid references affiliates(id) on delete set null
);

-- One live application per person. A rejected one doesn't block a better
-- second attempt later.
create unique index if not exists affiliate_applications_live_email_idx
  on affiliate_applications (email)
  where status <> 'rejected';

-- Two people cannot queue for the same code. Global uniqueness of live codes
-- is still owned by affiliates.code — this only stops the collision earlier,
-- while both are still applications.
create unique index if not exists affiliate_applications_live_code_idx
  on affiliate_applications (desired_code)
  where status <> 'rejected';

create index if not exists affiliate_applications_status_idx
  on affiliate_applications (status, created_at desc);

-- Service_role only. There is no signed-in user to scope this to (applicants
-- have no account), so the API is the only reader and writer.
alter table affiliate_applications enable row level security;

comment on table affiliate_applications is
  'Inbox of people asking for an affiliate link. Approval is manual and creates the affiliates row.';

-- --------------------------------------------------- approved-before-signup
-- An application approved for an email with no account keeps its code reserved
-- here. When that person finally signs up, this turns it into a real affiliate
-- without anyone touching it — otherwise their link would quietly stay dead
-- and the first thing they'd experience is being forgotten.
create or replace function link_affiliate_from_application()
returns trigger
language plpgsql
security definer
set search_path = public, auth
as $$
declare
  user_email text;
  app_row    affiliate_applications%rowtype;
  new_aff_id uuid;
begin
  select lower(u.email) into user_email from auth.users u where u.id = new.user_id;
  if user_email is null then
    return new;
  end if;

  select * into app_row
  from affiliate_applications
  where email = user_email and status = 'approved' and affiliate_id is null
  order by reviewed_at desc nulls last
  limit 1;
  if not found then
    return new;
  end if;

  -- Someone already an affiliate (a second account, a re-application) must not
  -- get a second row: affiliates.user_id is unique, so let the insert no-op.
  insert into affiliates (user_id, code, paypal_email, note)
  values (new.user_id, app_row.desired_code, app_row.paypal_email,
          'from application ' || app_row.id::text)
  on conflict do nothing
  returning id into new_aff_id;

  if new_aff_id is not null then
    update affiliate_applications
    set affiliate_id = new_aff_id
    where id = app_row.id;
  end if;

  return new;
end;
$$;

drop trigger if exists profiles_link_affiliate on profiles;
create trigger profiles_link_affiliate
  after insert on profiles
  for each row execute function link_affiliate_from_application();
