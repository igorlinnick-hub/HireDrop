"""Resume storage via Supabase Storage bucket `resumes`.

Convention: each user uploads to `<user_id>/resume.pdf`. The first folder
segment must equal auth.uid() — that's what the bucket's RLS policies
isolate on (see supabase-schema-v3.sql, PR 3.5 block).

Why a separate Storage owner: the previous design wrote the PDF to the
container's local filesystem (data/resume.pdf), which Railway wipes on
every redeploy. C7 in the refactor plan makes resumes persistent.
"""

from app.db.client import get_supabase

BUCKET = "resumes"
SIGNED_URL_TTL_SECONDS = 3600


def _path(user_id: str) -> str:
    return f"{user_id}/resume.pdf"


def upload(user_id: str, content: bytes) -> None:
    """Upsert the user's resume PDF into the bucket."""
    storage = get_supabase().storage.from_(BUCKET)
    storage.upload(
        path=_path(user_id),
        file=content,
        file_options={"content-type": "application/pdf", "upsert": "true"},
    )


def exists(user_id: str) -> bool:
    storage = get_supabase().storage.from_(BUCKET)
    items = storage.list(path=user_id) or []
    return any(item.get("name") == "resume.pdf" for item in items)


def signed_download_url(user_id: str) -> str | None:
    storage = get_supabase().storage.from_(BUCKET)
    if not exists(user_id):
        return None
    res = storage.create_signed_url(_path(user_id), SIGNED_URL_TTL_SECONDS)
    return res.get("signedURL") or res.get("signed_url")
