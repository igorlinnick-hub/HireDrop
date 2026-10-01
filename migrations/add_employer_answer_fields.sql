-- Three answers employers ask for that no profile field could give: school, degree and
-- what the candidate expects to be paid. All are asked once at signup
-- (modules/employer_answers.py) and read by the fillers.
--
-- School / degree. Live 2026-09-30: the first server-side submit (DoorDash) filled every
-- field on the form and stopped on "School" — there was nowhere it could have come from.
-- The night shift never recorded that stop as a hand-back, so the question never reached
-- the 30-day measurement that built the list either.
--
-- Salary expectation. The same night's walk met "What are your salary expectations?" on
-- 4 of 8 forms. content.js leaves it blank on purpose ("NEVER invent a number here") and
-- hands the form back; the server path asked the model instead, which told employers
-- $55k–$85k for a user whose own search floor is $100k. `salary_min` is a FILTER and
-- stays one — this column is what the user said to tell an employer, in their own words
-- ("$100,000 per year", "$35/hour").
--
-- The two booleans are ANSWERS, like `no_linkedin`: someone without a college degree,
-- or who will not name a figure, has answered the question — the filler then hands a
-- required field back honestly instead of inventing one.
alter table profiles add column if not exists school text default '';
alter table profiles add column if not exists degree text default '';
alter table profiles add column if not exists no_degree boolean default false;
alter table profiles add column if not exists salary_expectation text default '';
alter table profiles add column if not exists no_salary_expectation boolean default false;
