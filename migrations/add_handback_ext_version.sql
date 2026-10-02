-- Which extension build handed this job back (stamped by the server from
-- campaign_states.ext_version at POST /handbacks). A hand-back waits on the person only
-- while that same build is still the one running: a newer filler gets one retry, and if
-- it hits the same wall the row is re-stamped and waits again (queue, 10-02).
ALTER TABLE public.handbacks
  ADD COLUMN IF NOT EXISTS ext_version TEXT DEFAULT NULL;
