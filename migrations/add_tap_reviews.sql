-- Migration: tap-review relay — lets the PHONE approve applications the extension
-- prepared on the desktop. One row per user (the extension only ever has one card
-- pending at a time); the extension upserts the card, the phone reads it and writes
-- the decision, the extension polls the decision and submits on the computer.
-- Run in Supabase SQL editor: Dashboard → SQL Editor → New query → paste → Run

CREATE TABLE IF NOT EXISTS public.tap_reviews (
  user_id UUID PRIMARY KEY,
  review_id TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | skipped
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at TIMESTAMPTZ DEFAULT NULL
);

-- Backend uses the service role; no anon access needed.
ALTER TABLE public.tap_reviews ENABLE ROW LEVEL SECURITY;
