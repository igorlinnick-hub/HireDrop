-- Migration: the questions that stopped the application, and the answers the human
-- gives back — so a hand-back becomes one click instead of a trip to the job site.
--
-- Today a hand-back says "we couldn't finish this one" and links out. The extension
-- ALREADY knows more than that: collectUnfilledRequired() collects every required
-- field it could not fill and background.js sends the list to the activity log — but
-- POST /handbacks never carried it, so the durable to-do row lost exactly the part the
-- user needs. The data existed and was thrown away at the door.
--
-- Igor, 2026-09-21: "если нет инфы на вопрос то можно заполнить остальные 99% а этот
-- оставить на человека чтоб можно было одним кликом ему все завершить... кнопка
-- прогнать заявку и заявка переходит в тап очередь".
--
-- So the row gains three things:
--   * `questions` — what was asked and left blank (labels + option lists, as sent);
--   * `answers`   — what the human typed/picked, question-keyed;
--   * `job_id`    — the pool row, so answering can put it BACK in the approved queue
--                   (jobs.status = 'approved'), where an auto or tap run picks it up
--                   first (ext 1.8.6). Without the id the answers would have nowhere
--                   to be applied.
--
-- Why answers live HERE and not in screener_answer_cache: that cache is keyed by
-- question+options and deliberately holds CLOSED questions only, because an open
-- answer ("Why do you want to work here?") must differ per employer. These answers are
-- per-application by nature, which is the other axis.
--
-- Run: supabase db query --linked -f migrations/add_handback_questions.sql

ALTER TABLE public.handbacks
  -- [{label, options: [...]}, …] exactly as the filler saw them. jsonb, not text[],
  -- because a dropdown's option list has to survive the round trip or the dashboard
  -- cannot render the same choice the form offered.
  ADD COLUMN IF NOT EXISTS questions JSONB NOT NULL DEFAULT '[]'::jsonb,
  -- {"<question label>": "<answer>"} — written when the human answers, read by the
  -- filler on the retry.
  ADD COLUMN IF NOT EXISTS answers JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- The pool row this hand-back came from. Nullable: a native (Indeed/ZR) walk hands
  -- back jobs that were never pool rows, and those still belong in the list.
  ADD COLUMN IF NOT EXISTS job_id UUID DEFAULT NULL,
  -- Set when the answers are applied and the job is re-queued, so the UI can say
  -- "back in the queue" instead of leaving the row looking untouched.
  ADD COLUMN IF NOT EXISTS requeued_at TIMESTAMPTZ DEFAULT NULL;

-- The filler looks these up by job on every retry; without the index that is a table
-- scan per screener question.
CREATE INDEX IF NOT EXISTS handbacks_user_job_idx
  ON public.handbacks (user_id, job_id)
  WHERE job_id IS NOT NULL;
