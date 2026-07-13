-- Profile URL fields for ATS applications (Lever/Greenhouse ask for LinkedIn + portfolio).
-- Without these the filler leaves those required fields empty (see ROADMAP_E2E.md step 1).
-- Apply in the Supabase SQL editor.
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS linkedin_url  text DEFAULT '',
  ADD COLUMN IF NOT EXISTS portfolio_url text DEFAULT '';
