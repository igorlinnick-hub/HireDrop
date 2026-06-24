create table if not exists extension_status (
  user_id uuid primary key references auth.users(id) on delete cascade,
  ts float not null,
  campaign_running boolean default false,
  today_count integer default 0,
  window_visible boolean default false,
  last_screenshot_age float,
  error text,
  version text
);

alter table extension_status enable row level security;

create policy "Users manage own extension status"
  on extension_status for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);
