-- Migration: Add per-job tailored resume PDF URL to jobs table
-- Run in Supabase SQL editor: Dashboard → SQL Editor → New query → paste → Run

ALTER TABLE public.jobs
  ADD COLUMN IF NOT EXISTS tailored_resume_pdf_url TEXT DEFAULT NULL;
