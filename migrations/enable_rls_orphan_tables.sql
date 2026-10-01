-- 2026-09-29: Supabase advisor rls_disabled_in_public.
-- These tables had RLS off, so the public anon key could read/edit/delete them.
-- Backend uses service_role (bypasses RLS); no policies = anon/authenticated denied.
ALTER TABLE public.campaign_screenshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bot_settings         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conversations        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corrections          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.outbound_log         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.training_examples    ENABLE ROW LEVEL SECURITY;
