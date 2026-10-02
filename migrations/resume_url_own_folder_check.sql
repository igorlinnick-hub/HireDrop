-- resume_url is written by the browser (wizard upload via supabase-js), and RLS lets a
-- user update any column of their own row. Nothing stopped someone from saving another
-- user's "<their-uuid>/resume.pdf" as their own resume_url: the server reads that path
-- with the service key (no storage policy restrains it), so the victim's resume went out
-- as a file and as text inside the saver's cover letters and answers (#294 closed the read
-- side in app/db/profile.py::_own_path; this closes the write side at the source).
--
-- Every upload this product has ever made is "<user_id>/…" (live table: 15 of 15 rows),
-- so a path outside that folder is never a legitimate resume of this user's.
alter table profiles
  add constraint resume_url_own_folder
  check (resume_url is null or resume_url = '' or resume_url like user_id::text || '/%');
