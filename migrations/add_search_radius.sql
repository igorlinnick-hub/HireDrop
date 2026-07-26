-- Migration: optional search radius (miles) around the user's location.
-- Used by the dashboard filter row when location != remote — a circle of this
-- radius around the user's area. NULL = no radius filter.
-- Run in Supabase SQL editor: Dashboard → SQL Editor → New query → paste → Run

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS search_radius_miles INTEGER DEFAULT NULL;

COMMENT ON COLUMN public.profiles.search_radius_miles IS 'Optional non-remote search radius in miles (10/25/50/100); NULL = no radius filter';
