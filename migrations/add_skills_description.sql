-- The user's own words about their skills (free text, typed in Settings).
-- SOURCE hierarchy for the skills resume: the resume itself first, this
-- description fills what the resume lacks. Persisted so it survives
-- regenerations and pre-fills the describe-your-skills box every time.
alter table profiles add column if not exists skills_description text;
