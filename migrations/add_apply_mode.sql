-- Migration: Add apply mode + ideal job description to profiles
-- Run in Supabase SQL editor: Dashboard → SQL Editor → New query → paste → Run

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS apply_mode TEXT DEFAULT 'standard',
  ADD COLUMN IF NOT EXISTS ideal_job_description TEXT DEFAULT NULL;
