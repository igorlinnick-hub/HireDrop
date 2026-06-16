-- Migration: Add AI scoring columns to jobs table
-- Run in Supabase SQL editor: https://app.supabase.com → SQL Editor

ALTER TABLE jobs
  ADD COLUMN IF NOT EXISTS score        INTEGER,
  ADD COLUMN IF NOT EXISTS ai_verdict   TEXT,
  ADD COLUMN IF NOT EXISTS ai_flags     JSONB DEFAULT '[]';

-- Index for sorting by score on the dashboard
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs (user_id, score DESC);
