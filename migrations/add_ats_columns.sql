-- Migration: Add ATS scoring columns to jobs table

ALTER TABLE jobs
  ADD COLUMN IF NOT EXISTS ats_keywords  JSONB DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS ats_match_pct INTEGER DEFAULT 0;
