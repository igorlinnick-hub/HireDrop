-- The user's IANA time zone (browser-reported via /campaign/status), so the daily cap
-- rolls over at THEIR midnight instead of UTC's (decision 2026-09-27). See
-- app/db/user_day.py for how a zone change is kept from resetting the cap.
--
-- Its own table, RLS on and NO policies: only the backend (service role) reads or writes
-- it. A profiles column is writable by the user through supabase-js, which would let a
-- browser rewrite its zone at will.
create table if not exists user_timezones (
  user_id uuid primary key references auth.users(id) on delete cascade,
  zone text not null,
  day_anchor timestamptz,    -- start of the day in force when the zone last changed
  changed_at timestamptz not null default now()
);

-- First cut (10-05, never written to) stored prev_zone instead of the anchor.
alter table user_timezones add column if not exists day_anchor timestamptz;
alter table user_timezones drop column if exists prev_zone;

alter table user_timezones enable row level security;

-- The first cut of this feature added profiles.timezone (2026-10-05). Nothing ever wrote
-- it (the dashboard never sent a zone) and it is the user-writable place this table
-- replaces.
alter table profiles drop column if exists timezone;
