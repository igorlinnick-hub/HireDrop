-- Migration: Add tailored_resume column to jobs table
-- Run in Supabase SQL editor

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tailored_resume TEXT;
