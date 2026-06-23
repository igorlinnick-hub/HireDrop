-- Migration: Add ATS compliance columns to profiles table
-- Run in Supabase SQL editor: Dashboard → SQL Editor → New query → paste → Run

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS ats_score       INTEGER DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS ats_issues      JSONB   DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS ats_resume_url  TEXT    DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS ats_approved    BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS ats_checked_at  TIMESTAMPTZ DEFAULT NULL;
