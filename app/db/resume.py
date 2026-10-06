"""Resume storage via Supabase Storage bucket `resumes`.

Every object lives under `<user_id>/…`. The first folder segment must equal
auth.uid() — that's what the bucket's RLS policies isolate on (see
supabase-schema-v3.sql, PR 3.5 block).

Each upload gets a NEW name (`resume-<id>.pdf`, `resume_ats-<id>.pdf`, …) and the
profile column points at it. Until 10-06 every upload overwrote one fixed name, and
storage serves an object with `max-age=3600`: for seconds after a re-upload both a plain
download and a freshly signed URL could still return the PREVIOUS file — the resume the
user had just replaced went to an employer. A name nobody has fetched yet has nothing
cached. Rows written before that still point at the fixed names, which stay readable.

Why a separate Storage owner: the previous design wrote the PDF to the
container's local filesystem (data/resume.pdf), which Railway wipes on
every redeploy. C7 in the refactor plan makes resumes persistent.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta

from app.db.client import get_supabase

BUCKET = "resumes"
SIGNED_URL_TTL_SECONDS = 3600

# An older upload is deleted only once it is at least this old. The extension mints a
# signed URL and fetches it within a second, so a file replaced minutes ago is no longer
# being read — and a file this young may belong to a concurrent upload whose profile
# write hasn't landed yet.
STALE_AFTER = timedelta(minutes=10)

_PDF = "application/pdf"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Name stems per kind. "resume" never matches the others: they continue with "_", the
# fresh names with "-".
_ORIGINAL, _ATS, _SKILLS = "resume", "resume_ats", "resume_skills"


def _path(user_id: str) -> str:
    """Legacy fixed name of the original upload."""
    return f"{user_id}/resume.pdf"


def _ats_path(user_id: str) -> str:
    """Legacy fixed name of the ATS PDF."""
    return f"{user_id}/resume_ats.pdf"


def _skills_path(user_id: str) -> str:
    """Legacy fixed name of the skills PDF."""
    return f"{user_id}/resume_skills.pdf"


def _fresh_path(user_id: str, stem: str) -> str:
    return f"{user_id}/{stem}-{uuid.uuid4().hex[:12]}.pdf"


def _docx_twin(pdf_path: str) -> str:
    """The DOCX rendered alongside a generated PDF shares its name."""
    return pdf_path[: -len(".pdf")] + ".docx" if pdf_path.endswith(".pdf") else ""


def _own(user_id: str, path) -> str:
    from app.db.profile import _own_path

    return _own_path(user_id, path)


def _stored(user_id: str, column: str) -> str:
    """The path a profile column names — only inside this user's folder.

    These columns are writable by the user through supabase-js, and everything here
    signs with the service key, so a foreign path must read as "none" (see _own_path).
    """
    from app.db import profile as profile_db

    prof = profile_db.get_profile(user_id) or {}
    return _own(user_id, prof.get(column))


def _list(folder: str, name_prefix: str) -> list[dict]:
    """Objects in `folder` whose name starts with `name_prefix`.

    The listing pages at 100 sorted by name, and a folder also holds one
    `job_<id>_tailored.pdf` per tailored job (78 for one account on 10-06) — an
    unfiltered list would soon stop reaching the resume files at all.
    """
    return (
        get_supabase()
        .storage.from_(BUCKET)
        .list(path=folder, options={"search": name_prefix, "limit": 1000})
        or []
    )


def _object_exists(path: str) -> bool:
    """True if a specific object path exists in the bucket."""
    if not path:
        return False
    folder, _, name = path.rpartition("/")
    return any(item.get("name") == name for item in _list(folder, name))


def _upload(path: str, content: bytes, mime: str) -> str:
    get_supabase().storage.from_(BUCKET).upload(
        path=path,
        file=content,
        file_options={"content-type": mime, "upsert": "true"},
    )
    return path


def _stamp(item: dict) -> datetime | None:
    raw = item.get("updated_at") or item.get("created_at") or ""
    try:
        at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)  # storage stamps are UTC


def _prune(user_id: str, stem: str, current) -> None:
    """Delete this kind's older uploads, keeping the file `current()` resolves to (what
    the profile names now) and its DOCX twin. Runs BEFORE the new upload, so the file
    being replaced survives until the next one — a URL signed for it a moment ago still
    downloads. Never fatal: a failed read here must not fail an upload whose model call
    is already paid for."""
    pattern = re.compile(rf"^{re.escape(stem)}(-[0-9a-f]{{12}})?\.(pdf|docx)$")
    cutoff = datetime.now(UTC) - STALE_AFTER
    try:
        keep = current() or ""
        kept = {keep, _docx_twin(keep)}
        stale = []
        for item in _list(user_id, stem):
            name = item.get("name") or ""
            path = f"{user_id}/{name}"
            if not pattern.match(name) or path in kept:
                continue
            at = _stamp(item)
            if at and at < cutoff:  # no readable age — leave it
                stale.append(path)
        if stale:
            get_supabase().storage.from_(BUCKET).remove(stale)
    except Exception as e:
        print(f"[resume] prune {stem} for {user_id} failed (non-fatal): {e}")


def upload(user_id: str, content: bytes) -> str:
    """Store a new original resume PDF and point profiles.resume_url at it."""
    _prune(user_id, _ORIGINAL, lambda: resolved_resume_path(user_id))
    path = _upload(_fresh_path(user_id, _ORIGINAL), content, _PDF)
    # Only this column — profile_db.update_profile resets name/keywords when omitted.
    get_supabase().table("profiles").update({"resume_url": path}).eq("user_id", user_id).execute()
    return path


def upload_ats(user_id: str, content: bytes) -> str:
    """Store a new ATS-optimized PDF. Returns its path; the caller saves it as
    profiles.ats_resume_url."""
    _prune(user_id, _ATS, lambda: _resolved_ats(user_id))
    return _upload(_fresh_path(user_id, _ATS), content, _PDF)


def upload_ats_docx(user_id: str, content: bytes, pdf_path: str) -> str:
    """Store the DOCX twin of the ATS PDF at `pdf_path`. Returns its path.

    DOCX is offered alongside the PDF for job boards / employers that only
    accept Word documents.
    """
    return _upload(_docx_twin(pdf_path), content, _DOCX_MIME)


def upload_skills(user_id: str, content: bytes) -> str:
    """Store a new skills-style PDF. Returns its path; the caller saves it as
    profiles.skills_resume_url."""
    _prune(user_id, _SKILLS, lambda: _resolved_skills(user_id))
    return _upload(_fresh_path(user_id, _SKILLS), content, _PDF)


def upload_skills_docx(user_id: str, content: bytes, pdf_path: str) -> str:
    """Store the DOCX twin of the skills PDF at `pdf_path`. Returns its path."""
    return _upload(_docx_twin(pdf_path), content, _DOCX_MIME)


def resolved_resume_path(user_id: str) -> str | None:
    """The user's actual original-resume path.

    The source of truth is profiles.resume_url — uploads get a fresh name each time,
    and older onboarding kept the file's original name (e.g.
    `<uid>/Ihor_Linnyk_Resume.pdf`), so we must NOT assume `<uid>/resume.pdf`.
    Falls back to that legacy fixed path for older accounts. Returns None if no
    object is found.
    """
    path = _stored(user_id, "resume_url")
    if path and _object_exists(path):
        return path
    legacy = _path(user_id)
    if _object_exists(legacy):
        return legacy
    return None


def _resolved_generated(user_id: str, column: str, legacy: str) -> str | None:
    path = _stored(user_id, column)
    if path and _object_exists(path):
        return path
    if _object_exists(legacy):
        return legacy
    return None


def _resolved_ats(user_id: str) -> str | None:
    return _resolved_generated(user_id, "ats_resume_url", _ats_path(user_id))


def _resolved_skills(user_id: str) -> str | None:
    return _resolved_generated(user_id, "skills_resume_url", _skills_path(user_id))


def _resolved_twin(pdf_path: str | None) -> str | None:
    docx = _docx_twin(pdf_path or "")
    return docx if docx and _object_exists(docx) else None


def _sign(path: str | None) -> str | None:
    if not path:
        return None
    res = get_supabase().storage.from_(BUCKET).create_signed_url(path, SIGNED_URL_TTL_SECONDS)
    return res.get("signedURL") or res.get("signed_url")


def exists(user_id: str) -> bool:
    return resolved_resume_path(user_id) is not None


def exists_ats(user_id: str) -> bool:
    return _resolved_ats(user_id) is not None


def exists_ats_docx(user_id: str) -> bool:
    return _resolved_twin(_resolved_ats(user_id)) is not None


def signed_download_url(user_id: str) -> str | None:
    return _sign(resolved_resume_path(user_id))


def signed_download_url_ats(user_id: str) -> str | None:
    return _sign(_resolved_ats(user_id))


def signed_download_url_ats_docx(user_id: str) -> str | None:
    return _sign(_resolved_twin(_resolved_ats(user_id)))


def signed_download_url_skills(user_id: str) -> str | None:
    return _sign(_resolved_skills(user_id))


def signed_download_url_skills_docx(user_id: str) -> str | None:
    return _sign(_resolved_twin(_resolved_skills(user_id)))


def best_signed_url(
    user_id: str, ats_approved: bool, default_resume: str | None = None
) -> str | None:
    """The user's base resume for applying.

    `default_resume` ('original' | 'ats' | 'skills') is THE authority when set —
    one dial, one owner (profiles.default_resume). `ats_approved` is the legacy
    fallback for profiles that never touched the dial; keeping both authorities
    live at once is exactly the two-masters bug the fit engine already paid for.
    Missing generated files fall back to the original rather than 404ing an
    apply in progress.
    """
    if default_resume == "skills":
        url = signed_download_url_skills(user_id)
        if url:
            return url
    elif default_resume == "ats" or (default_resume is None and ats_approved):
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


def signed_url_from_path(path: str, user_id: str | None = None) -> str | None:
    """Create a signed URL for a path in the resumes bucket.

    Bucket paths are `<user_id>/…`. When user_id is given (all real callers do), REFUSE
    to sign a path outside that user's prefix — so this can never mint a URL to another
    user's resume even if a request-supplied path ever reaches it (defense-in-depth;
    service_role bypasses storage RLS)."""
    if not path:
        return None
    if user_id and not (path == f"{user_id}" or path.startswith(f"{user_id}/")):
        print(f"[resume] refused signed URL: path '{path}' not owned by {user_id}")
        return None
    res = get_supabase().storage.from_(BUCKET).create_signed_url(path, SIGNED_URL_TTL_SECONDS)
    return res.get("signedURL") or res.get("signed_url")
