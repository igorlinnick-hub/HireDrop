-- Drop the clarifier: one question per posting per person, enforced by the database.
-- Two dashboard loads at once (two tabs, a reload) both find no question for today and both
-- pick the same posting; without this both inserts land and the person is asked twice.
-- app/routers/buddy.py reads a 23505 here as "the other load asked it" and returns that row.
create unique index if not exists fit_clarifications_user_job_uidx
  on fit_clarifications (user_id, job_id)
  where job_id is not null;
