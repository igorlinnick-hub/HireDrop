-- Work setting (remote | hybrid | onsite, '' = any) is the one dashboard filter that
-- had nowhere to live. It was held in React state and sent with the START payload, so
-- it survived exactly as long as the tab did: every Stop, reload or campaign reset put
-- the chip back to "Any setting" and the user re-picked it (Igor, 2026-09-21).
--
-- NULL and '' both mean "no filter", same convention job_type already uses — a real
-- answer, not a blank, so nothing here defaults it to a narrowing nobody asked for.
alter table profiles add column if not exists work_setting text;
