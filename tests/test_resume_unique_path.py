"""Every resume upload gets a fresh storage name (10-06).

Storage serves objects with max-age=3600: re-uploading over one fixed name left a
window where a freshly signed URL still returned the PREVIOUS file to an employer.
These tests pin the contract: a new name per upload, the profile column is the
authority (own folder only), legacy fixed names still read, and older uploads are
pruned without ever removing the file being replaced or one younger than STALE_AFTER.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.db import resume as resume_storage

ME = "11111111-1111-1111-1111-111111111111"
VICTIM = "22222222-2222-2222-2222-222222222222"

OLD = (datetime.now(UTC) - timedelta(days=3)).isoformat().replace("+00:00", "Z")
NEW = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")


class FakeBucket:
    """The slice of storage3's bucket API resume.py touches."""

    def __init__(self, objects: dict[str, str]):
        self.objects = dict(objects)  # path -> updated_at
        self.removed: list[str] = []
        self.list_calls: list[tuple] = []

    def list(self, path=None, options=None):
        options = options or {}
        self.list_calls.append((path, options))
        prefix = f"{path}/"
        search = options.get("search", "")
        out = []
        for p, at in sorted(self.objects.items()):
            if p.startswith(prefix) and "/" not in p[len(prefix) :]:
                name = p[len(prefix) :]
                if name.startswith(search):
                    out.append({"name": name, "updated_at": at})
        return out[: options.get("limit", 100)]

    def upload(self, path, file, file_options=None):
        self.objects[path] = datetime.now(UTC).isoformat()

    def remove(self, paths):
        self.removed.extend(paths)
        for p in paths:
            self.objects.pop(p, None)

    def create_signed_url(self, path, ttl):
        return {"signedURL": f"https://signed/{path}"}


def _client(bucket: FakeBucket, profile_row: dict):
    client = MagicMock()
    client.storage.from_.return_value = bucket
    client.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[profile_row]
    )
    return client


@pytest.fixture
def env():
    def make(objects: dict[str, str], **row):
        bucket = FakeBucket(objects)
        client = _client(bucket, {"user_id": ME, **row})
        patches = [
            patch("app.db.resume.get_supabase", return_value=client),
            patch("app.db.profile.get_supabase", return_value=client),
        ]
        for p in patches:
            p.start()
        make.patches.extend(patches)
        return bucket, client

    make.patches = []
    yield make
    for p in make.patches:
        p.stop()


def test_each_ats_upload_gets_a_name_nobody_has_fetched(env):
    bucket, _ = env({f"{ME}/resume_ats.pdf": OLD}, ats_resume_url=f"{ME}/resume_ats.pdf")

    first = resume_storage.upload_ats(ME, b"%PDF-a")
    second = resume_storage.upload_ats(ME, b"%PDF-b")

    assert first != second
    for path in (first, second):
        assert path.startswith(f"{ME}/resume_ats-") and path.endswith(".pdf")
    assert resume_storage.upload_ats_docx(ME, b"docx", first) == first[:-4] + ".docx"


def test_readers_follow_the_profile_column_not_the_fixed_name(env):
    fresh = f"{ME}/resume_ats-0123456789ab.pdf"
    env(
        {f"{ME}/resume_ats.pdf": OLD, fresh: NEW, fresh[:-4] + ".docx": NEW},
        ats_resume_url=fresh,
    )

    assert resume_storage.signed_download_url_ats(ME) == f"https://signed/{fresh}"
    assert resume_storage.signed_download_url_ats_docx(ME) == f"https://signed/{fresh[:-4]}.docx"
    assert resume_storage.exists_ats(ME) and resume_storage.exists_ats_docx(ME)


def test_legacy_fixed_names_still_resolve(env):
    """Every row written before 10-06 points at (or omits) the fixed names."""
    env(
        {
            f"{ME}/resume.pdf": OLD,
            f"{ME}/resume_skills.pdf": OLD,
            f"{ME}/resume_skills.docx": OLD,
        },
        resume_url="",
        skills_resume_url=None,
    )

    assert resume_storage.signed_download_url(ME) == f"https://signed/{ME}/resume.pdf"
    assert resume_storage.signed_download_url_skills(ME) == f"https://signed/{ME}/resume_skills.pdf"
    assert (
        resume_storage.signed_download_url_skills_docx(ME)
        == f"https://signed/{ME}/resume_skills.docx"
    )
    assert resume_storage.signed_download_url_ats(ME) is None


def test_a_foreign_path_in_a_generated_column_is_never_signed(env):
    """ats_/skills_resume_url are writable by the user (RLS: own row, any column);
    resume.py signs with the service key, so another user's path must read as none."""
    bucket, _ = env(
        {f"{VICTIM}/resume_ats-0123456789ab.pdf": OLD, f"{VICTIM}/resume_skills.pdf": OLD},
        ats_resume_url=f"{VICTIM}/resume_ats-0123456789ab.pdf",
        skills_resume_url=f"{VICTIM}/resume_skills.pdf",
    )

    assert resume_storage.signed_download_url_ats(ME) is None
    assert resume_storage.signed_download_url_skills(ME) is None
    assert all(path == ME for path, _ in bucket.list_calls)


def test_prune_keeps_the_file_being_replaced_and_anything_young(env):
    current = f"{ME}/resume_ats-aaaaaaaaaaaa.pdf"
    older = f"{ME}/resume_ats-bbbbbbbbbbbb.pdf"
    young = f"{ME}/resume_ats-cccccccccccc.pdf"
    bucket, _ = env(
        {
            current: OLD,
            current[:-4] + ".docx": OLD,
            older: OLD,
            older[:-4] + ".docx": OLD,
            young: NEW,  # a concurrent upload whose profile write hasn't landed
            f"{ME}/resume_ats.pdf": OLD,  # the legacy name, no longer current
            f"{ME}/resume_skills.pdf": OLD,  # another kind
            f"{ME}/resume.pdf": OLD,  # another kind
            f"{ME}/job_9_tailored.pdf": OLD,  # per-job, not ours to prune
        },
        ats_resume_url=current,
    )

    resume_storage.upload_ats(ME, b"%PDF-new")

    assert sorted(bucket.removed) == sorted([older, older[:-4] + ".docx", f"{ME}/resume_ats.pdf"])
    assert current in bucket.objects and current[:-4] + ".docx" in bucket.objects
    assert young in bucket.objects


def test_original_upload_writes_only_resume_url(env):
    """profile_db.update_profile resets name/keywords when omitted — upload() must not
    go through it."""
    bucket, client = env({f"{ME}/resume.pdf": OLD}, resume_url="")

    path = resume_storage.upload(ME, b"%PDF-x")

    assert path.startswith(f"{ME}/resume-") and path in bucket.objects
    client.table.return_value.update.assert_called_once_with({"resume_url": path})
    # The legacy file was the resolved current one: it survives this upload.
    assert f"{ME}/resume.pdf" in bucket.objects and not bucket.removed


def test_original_prune_never_touches_a_kept_original_name(env):
    """Older onboarding stored the user's own filename; it isn't a fresh/legacy name."""
    named = f"{ME}/Ihor_Linnyk_Resume.pdf"
    bucket, _ = env({named: OLD, f"{ME}/resume.pdf": OLD}, resume_url=named)

    resume_storage.upload(ME, b"%PDF-x")

    assert bucket.removed == [f"{ME}/resume.pdf"]
    assert named in bucket.objects


def test_existence_check_survives_a_folder_full_of_tailored_pdfs(env):
    """The listing pages at 100 sorted by name; job_* sorts before resume*."""
    objects = {f"{ME}/job_{i:04d}_tailored.pdf": OLD for i in range(150)}
    objects[f"{ME}/resume_ats.pdf"] = OLD
    env(objects, ats_resume_url=f"{ME}/resume_ats.pdf")

    assert resume_storage.exists_ats(ME)


def test_a_failing_prune_never_blocks_the_upload(env):
    bucket, _ = env({f"{ME}/resume_ats-bbbbbbbbbbbb.pdf": OLD}, ats_resume_url="")

    def boom(paths):
        raise RuntimeError("storage down")

    bucket.remove = boom
    path = resume_storage.upload_ats(ME, b"%PDF-new")

    assert path in bucket.objects


def test_a_failing_read_before_the_upload_never_blocks_it(env):
    """The prune's lookup runs before the upload; ats generation has already paid for
    the model call by then, so a storage/profile read failure must not 500 it."""
    bucket, _ = env({}, ats_resume_url="")

    def down(*a, **k):
        raise RuntimeError("storage list 503")

    bucket.list = down
    path = resume_storage.upload_ats(ME, b"%PDF-new")

    assert path in bucket.objects


def test_a_stamp_without_a_zone_is_read_as_utc_not_a_crash(env):
    naive = (datetime.now(UTC) - timedelta(days=3)).replace(tzinfo=None).isoformat()
    old_one = f"{ME}/resume_ats-bbbbbbbbbbbb.pdf"
    bucket, _ = env({old_one: naive, f"{ME}/resume_ats-cccccccccccc.pdf": OLD}, ats_resume_url="")

    resume_storage.upload_ats(ME, b"%PDF-new")

    assert sorted(bucket.removed) == sorted([old_one, f"{ME}/resume_ats-cccccccccccc.pdf"])
