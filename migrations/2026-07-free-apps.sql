-- Free taste (FREE_TASTE_PLAN.md): lifetime counter of applications made on the
-- free tier. check_can_apply() denies free users at FREE_APP_LIMIT (config.py);
-- the counter only ever moves through the RPC below, called by the backend
-- (service_role) after a real application save — clients can never touch it.

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS free_apps_used integer NOT NULL DEFAULT 0;

-- Atomic increment. UPSERT rather than bare UPDATE: accounts created before the
-- handle_new_user trigger existed (pre 2026-07-12) may have no profiles row yet —
-- a bare UPDATE would silently no-op and hand them an uncounted (infinite) taste.
-- profiles.user_id is UNIQUE, so ON CONFLICT targets it directly.
CREATE OR REPLACE FUNCTION public.increment_free_apps(p_user_id uuid)
RETURNS integer LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  INSERT INTO public.profiles (user_id, free_apps_used)
  VALUES (p_user_id, 1)
  ON CONFLICT (user_id)
  DO UPDATE SET free_apps_used = public.profiles.free_apps_used + 1
  RETURNING free_apps_used;
$$;

-- Backend-only (service_role bypasses grants); no client may bump the counter.
REVOKE EXECUTE ON FUNCTION public.increment_free_apps(uuid) FROM public, anon, authenticated;
