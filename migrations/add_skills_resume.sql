-- Skills-style resume (hybrid: compact chronology + grouped skills) + explicit
-- default-resume dial. default_resume is THE authority when set ('original' |
-- 'ats' | 'skills'); NULL keeps the legacy behavior (ats_approved → ATS, else
-- original) so existing users are untouched. skill_groups stores the grouped
-- skills produced by the skills-resume structuring, source = the resume itself.
alter table profiles add column if not exists skills_resume_url text;
alter table profiles add column if not exists skill_groups jsonb;
alter table profiles add column if not exists default_resume text
  check (default_resume in ('original', 'ats', 'skills'));
