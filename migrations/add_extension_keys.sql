-- Durable per-user extension API keys (Approach A — "agent credential").
-- The extension holds an opaque, non-rotating key and uses it for all backend calls,
-- decoupling it from the dashboard-pushed Supabase access token (which expired when the
-- dashboard tab was closed). We store only the SHA-256 hash + a short prefix (never the
-- raw key). Revocation = set revoked_at (logout / password-change / disconnect / re-issue).
create table if not exists public.extension_keys (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  email       text,                         -- cached so verify() needn't hit auth admin
  key_hash    text not null,                -- sha256(raw), hex
  prefix      text not null,                -- first chars of raw, for fast lookup
  created_at  timestamptz not null default now(),
  last_used_at timestamptz,
  revoked_at  timestamptz
);

create index if not exists extension_keys_prefix_idx on public.extension_keys (prefix);
create index if not exists extension_keys_user_idx   on public.extension_keys (user_id);

-- Backend uses the service_role key (bypasses RLS); no policies needed. Never expose this
-- table to the anon/authenticated roles.
