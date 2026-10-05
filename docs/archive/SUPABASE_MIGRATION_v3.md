# Supabase migration v3 — runbook

This migration unblocks PRs 3.4b, 3.5, 4.1, 4.2. It adds 3 tables, seeds Indeed selectors, and creates Storage policies for resume uploads. Idempotent.

## Steps

### 1. Create the Storage bucket (UI, ~30 sec)

1. Open Supabase Dashboard → **Storage** in the left sidebar.
2. Click **"New bucket"**.
3. Name: `resumes`.
4. **Public bucket**: **OFF** (uncheck — we want RLS-only access).
5. Click **Save**.

### 2. Run the SQL (Dashboard SQL Editor, ~10 sec)

1. Open Supabase Dashboard → **SQL Editor** → **+ New query**.
2. Open `supabase-schema-v3.sql` in this repo, copy the **entire contents**.
3. Paste into the SQL editor.
4. Click **Run** (or press Cmd/Ctrl+Enter).
5. Expected: `Success. No rows returned.` (or rows from the last `select` lines if you uncomment them).

### 3. Verify

In the same SQL editor, run:

```sql
select count(*) from public.cover_letter_usage;
select platform, version from public.platform_selectors;
select count(*) from public.activity_log;
select id, name, public from storage.buckets where id = 'resumes';
```

Expected output:
- `cover_letter_usage` → 0
- `platform_selectors` → one row, `indeed` v1
- `activity_log` → 0
- buckets → one row, `resumes`, public = false

### 4. Confirm to Claude

Reply "schema applied" (or anything affirmative) — Claude will then push backend PRs 3.4b, 3.5, 4.1, 4.2 in sequence, each tested and merged.

## What this gives you

| Table / bucket | Used by | Why |
|---|---|---|
| `cover_letter_usage` | PR 3.4b | Per-user daily quota on Anthropic API (50/day). Stops a single user from running up the bill. |
| `resumes` bucket | PR 3.5 | Persistent resume storage. Today the resume PDF lives on Railway's ephemeral disk and disappears on every redeploy. |
| `platform_selectors` | PR 4.1 | DOM selectors for Indeed, etc. live as data, not code. When Indeed changes their layout you update one row, no extension rebuild. |
| `activity_log` | PR 4.2 | Central observability. Every campaign action gets a trace; users see the log in their dashboard. |

## Rollback

Each migration block is independent. To roll back any one:

```sql
-- PR 3.4b
drop table if exists public.cover_letter_usage cascade;

-- PR 4.1
drop table if exists public.platform_selectors cascade;

-- PR 4.2
drop table if exists public.activity_log cascade;

-- PR 3.5
drop policy if exists "users_own_resumes_select" on storage.objects;
drop policy if exists "users_own_resumes_insert" on storage.objects;
drop policy if exists "users_own_resumes_update" on storage.objects;
drop policy if exists "users_own_resumes_delete" on storage.objects;
-- Then delete the bucket via Storage UI.
```
