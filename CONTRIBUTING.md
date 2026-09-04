# Contributing

## Branch naming

`<phase>/<short-slug>` — e.g. `phase-3.5/resume-storage`, `chore/quality-hygiene`, `fix/cover-letter-fallback`.

## Per-PR contract

Every PR must:
1. Pass `ruff check .` and `ruff format --check .`.
2. Pass `pytest tests/` with coverage ≥ the current `--cov-fail-under` threshold.
3. Have a description that includes a **Test plan** section with at least one verification step.
4. Be small enough to reason about in one sitting. Big features fan out into a sequence of PRs (`phase-X.1`, `phase-X.2`, …).

## Commit messages

First line ≤ 72 chars, imperative. Body explains *why*, not *what*. Reference relevant `app/`, `modules/`, or `chrome-extension/` paths so reviewers don't have to search.

## Schema changes

1. Add the migration to a new `supabase-schema-vN.sql` (idempotent — `IF NOT EXISTS`, `do nothing on conflict`).
2. Apply via the Management API (`SUPABASE_PAT` in `.env`):
   ```python
   import os, requests
   from pathlib import Path

   for line in Path(".env").read_text().splitlines():
       if "=" in line and not line.startswith("#"):
           k, v = line.split("=", 1)
           os.environ[k] = v
   r = requests.post(
       f"https://api.supabase.com/v1/projects/{os.environ['SUPABASE_PROJECT_REF']}/database/query",
       headers={"Authorization": f"Bearer {os.environ['SUPABASE_PAT']}"},
       json={"query": Path("supabase-schema-vN.sql").read_text()},
   )
   print(r.status_code, r.text[:200])
   ```
3. Update `SUPABASE_MIGRATION_vN.md` with rollback SQL.
4. Code that depends on the new schema ships in a *separate* PR after the migration is applied to prod.

## Smoke checklist (run after every prod deploy)

1. `curl https://web-production-db45.up.railway.app/health` → `{"status":"ok"}`
2. Log into the website, profile loads
3. `POST /jobs/find` twice with the same profile → second call returns `count: 0` (Supabase dedup)
4. Upload a resume PDF → `GET /profile/resume/status` returns `uploaded: true` → signed URL fetches the file
5. Run a campaign on a sandbox Indeed account → counter increments **only** on verified submissions
6. After campaign stop, `chrome.declarativeNetRequest.getSessionRules` returns `[]` (UA override cleared)

## Don't merge if

- Any of the above smoke checks fails locally
- `pyright app modules` introduces *new* errors above the current count (203 baseline — track it)
- The change touches `auth/`, `db/client.py`, RLS policies, or the bucket without an explicit review nod
