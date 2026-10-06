-- What the user's OWN uploaded resume states for the employer questions — city, state,
-- latest title and employer, LinkedIn, school, degree (modules/ai_resume_facts.py).
-- Offered in the answers form for the person to confirm; nothing here is an answer.
--
-- Shape: {"resume_url": "<the profiles.resume_url it was read from>",
--         "facts": {"school": "...", ...}, "extracted_at": "<iso>"}
--
-- Why a column: the read was cached in process memory for 6h, so every deploy and each
-- of the two workers paid for it again. Keyed by resume_url, and every upload gets a new
-- file name (#367), so a new resume is read afresh without anything clearing this.
-- The code works without the column (it just reads again), so it is safe in any order.
alter table profiles add column if not exists resume_facts jsonb;

notify pgrst, 'reload schema';
