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


def _ats_path(user_id: str) -> str:
    return f"{user_id}/resume_ats.pdf"


def upload(user_id: str, content: bytes) -> None:
    """Upsert the user's resume PDF into the bucket."""
    storage = get_supabase().storage.from_(BUCKET)
    storage.upload(
        path=_path(user_id),
        file=content,
        file_options={"content-type": "application/pdf", "upsert": "true"},
    )


def upload_ats(user_id: str, content: bytes) -> str:
    """Upsert the ATS-optimized resume PDF. Returns the storage path."""
    storage = get_supabase().storage.from_(BUCKET)
    storage.upload(
        path=_ats_path(user_id),
        file=content,
        file_options={"content-type": "application/pdf", "upsert": "true"},
    )
    return _ats_path(user_id)


def exists(user_id: str) -> bool:
    storage = get_supabase().storage.from_(BUCKET)
    items = storage.list(path=user_id) or []
    return any(item.get("name") == "resume.pdf" for item in items)


def exists_ats(user_id: str) -> bool:
    storage = get_supabase().storage.from_(BUCKET)
    items = storage.list(path=user_id) or []
    return any(item.get("name") == "resume_ats.pdf" for item in items)


def signed_download_url(user_id: str) -> str | None:
    storage = get_supabase().storage.from_(BUCKET)
    if not exists(user_id):
        return None
    res = storage.create_signed_url(_path(user_id), SIGNED_URL_TTL_SECONDS)
    return res.get("signedURL") or res.get("signed_url")


def signed_download_url_ats(user_id: str) -> str | None:
    storage = get_supabase().storage.from_(BUCKET)
    if not exists_ats(user_id):
        return None
    res = storage.create_signed_url(_ats_path(user_id), SIGNED_URL_TTL_SECONDS)
    return res.get("signedURL") or res.get("signed_url")


def best_signed_url(user_id: str, ats_approved: bool) -> str | None:
    """Return the ATS resume URL if approved, else the original."""
    if ats_approved:
        url = signed_download_url_ats(user_id)
        if url:
            return url
    return signed_download_url(user_id)


def _job_tailored_path(user_id: str, job_id: str) -> str:
    return f"{user_id}/job_{job_id}_tailored.pdf"


def upload_job_tailored(user_id: str, job_id: str, content: bytes) -> str:
    """Upload a per-job tailored ATS PDF. Returns the storage path."""
    path = _job_tailored_path(user_id, job_id)
    get_supabase().storage.from_(BUCKET).upload(
        path=path,
        file=content,
        file_options={"content-type": "application/pdf", "upsert": "true"},
    )
    return path


def signed_url_from_path(path: str) -> str | None:
    """Create a signed URL for any path in the resumes bucket."""
    res = get_supabase().storage.from_(BUCKET).create_signed_url(path, SIGNED_URL_TTL_SECONDS)
    return res.get("signedURL") or res.get("signed_url")
