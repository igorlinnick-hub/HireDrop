-- Campaign screenshots: survives Railway restarts (replaces in-memory dict)
-- user_id is PK so only one row per user — upsert overwrites on every POST
CREATE TABLE IF NOT EXISTS campaign_screenshots (
    user_id UUID PRIMARY KEY,
    data    TEXT        NOT NULL,
    ts      FLOAT8      NOT NULL
);

-- No RLS needed — backend accesses this via service key only.
-- Extension POSTs via authenticated API; data never exposed to other users.
