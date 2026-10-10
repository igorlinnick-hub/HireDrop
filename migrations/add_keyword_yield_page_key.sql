-- The extension sends one results page in several requests; they share a page key so the
-- chunks count as one page (app/db/keyword_yield.recent). Null = one request, one page.
alter table keyword_yield add column if not exists page_key text;
