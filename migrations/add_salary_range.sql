-- Migration: Add optional salary-range filter to profiles
-- Run in Supabase SQL editor: Dashboard → SQL Editor → New query → paste → Run

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS salary_min INTEGER DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS salary_max INTEGER DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS salary_listed_only BOOLEAN DEFAULT FALSE;

COMMENT ON COLUMN public.profiles.salary_min IS 'Optional fit filter: minimum annual USD salary';
COMMENT ON COLUMN public.profiles.salary_max IS 'Optional fit filter: maximum annual USD salary';
COMMENT ON COLUMN public.profiles.salary_listed_only IS 'Only apply to jobs that list a salary (default off)';
