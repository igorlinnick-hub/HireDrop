-- Heartbeat TTL for zombie-campaign self-healing (ZOMBIE_FIX_PLAN.md).
-- The extension's 60s /extension/ping stamps this; /campaign/status treats a running
-- flag with a ping older than ~150s as NOT running (and lazily flips it).
-- Run once in the Supabase SQL editor:
alter table public.campaign_states add column if not exists last_ping_at timestamptz;
