-- Which extension build each user's browser is actually running, reported by
-- /extension/ping. The store-published version lags the repo silently: users ran
-- 1.8.3 for weeks while the repo was at 1.8.15 (found via Antonia's log, 09-23),
-- and the only trace was a substring in activity-log lines. One durable column
-- per user makes the fleet's version spread a query, not an archaeology dig.
alter table campaign_states add column if not exists ext_version text;
alter table campaign_states add column if not exists ext_version_at timestamptz;
