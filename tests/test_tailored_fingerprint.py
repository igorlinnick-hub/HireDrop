"""A per-job tailoring is tied to the resume it was built from (10-06).

The tailored PDF stays on its job row forever. Before this, a user who replaced their
resume (or switched the default one) still had every job tailored earlier sent with
the version built from the old resume. The resume fingerprint now lives in the file
name; a mismatch re-tailors (paid tier) or falls back to the base resume.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.db import resume as resume_storage
from app.routers import profile as profile_router

UID = "11111111-1111-1111-1111-111111111111"
NOW_FP = "aaaaaaaaaaaa"
OLD_FP = "bbbbbbbbbbbb"
CURRENT = f"{UID}/job_j1_tailored-{NOW_FP}.pdf"
STALE = f"{UID}/job_j1_tailored-{OLD_FP}.pdf"
UNKNOWN = f"{UID}/job_j1_tailored-unknown.pdf"
LEGACY = f"{UID}/job_j1_tailored.pdf"


@pytest.mark.parametrize(
    ("path", "fingerprint", "current"),
    [
        (CURRENT, NOW_FP, True),
        (STALE, NOW_FP, False),
        (STALE, None, True),  # current resume can't be identified: can't tell
        (UNKNOWN, NOW_FP, False),  # made blind → redone once it can be told
        (UNKNOWN, None, True),
        (None, NOW_FP, False),
    ],
)
def test_named_tailorings(path, fingerprint, current):
    assert resume_storage.tailoring_is_current(UID, path, {}, fingerprint) is current


def _stamp(dt):
    return dt.isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize(
    ("pdf_age_days", "resume_age_days", "current"),
    [(1, 3, True), (3, 1, False), (None, 1, True)],
)
def test_pre_fingerprint_tailorings_are_judged_by_upload_time(
    pdf_age_days, resume_age_days, current
):
    now = datetime.now(UTC)
    items = {f"{UID}/r-aaaaaaaaaaaa.pdf": _stamp(now - timedelta(days=resume_age_days))}
    if pdf_age_days is not None:
        items[LEGACY] = _stamp(now - timedelta(days=pdf_age_days))

    def fake_list(folder, name):
        return [
            {"name": p.rpartition("/")[2], "updated_at": at}
            for p, at in items.items()
            if p.rpartition("/")[2].startswith(name)
        ]

    profile = {"resume_url": f"{UID}/r-aaaaaaaaaaaa.pdf"}
    with patch.object(resume_storage, "_list", side_effect=fake_list):
        assert resume_storage.tailoring_is_current(UID, LEGACY, profile, NOW_FP) is current


def test_the_fingerprint_follows_the_text_tailoring_reads():
    seen = []

    def identity(profile):
        seen.append(profile["default_resume"])
        return f"{profile['default_resume']}|etag"

    with patch("app.db.screener_cache.resume_identity", side_effect=identity):
        original = resume_storage.tailor_fingerprint({"default_resume": "original"})
        skills = resume_storage.tailor_fingerprint({"default_resume": "skills"})
        ats_bare = resume_storage.tailor_fingerprint({"default_resume": "ats"})
        ats = resume_storage.tailor_fingerprint(
            {"default_resume": "ats", "ats_structure": {"a": 1}}
        )
    # skills and a structure-less ATS resume read the uploaded file, like the original
    assert original == skills == ats_bare != ats and len(ats) == 12
    with patch("app.db.screener_cache.resume_identity", return_value=None):
        assert resume_storage.tailor_fingerprint({}) is None


@pytest.mark.parametrize(("fp", "path"), [(NOW_FP, CURRENT), (None, UNKNOWN)])
def test_upload_names_the_file_by_fingerprint(fp, path):
    bucket = MagicMock()
    with patch("app.db.resume.get_supabase") as sb:
        sb.return_value.storage.from_.return_value = bucket
        assert resume_storage.upload_job_tailored(UID, "j1", b"%PDF-", fp) == path
    assert bucket.upload.call_args.kwargs["path"] == path


def _job(pdf=None, text=None, desc="x" * 400):
    return {
        "id": "j1",
        "description": desc,
        "tailored_resume_pdf_url": pdf,
        "tailored_resume": text,
    }


def _run(job, tier="pro", store_result=CURRENT, store_error=None):
    user = type("U", (), {"id": UID, "email": "x@y.z"})()
    tailor = MagicMock(return_value="NEW TAILORED")
    store = MagicMock(return_value=store_result, side_effect=store_error)
    clear, remove = MagicMock(), MagicMock()
    fp = MagicMock(return_value=NOW_FP)
    with (
        patch("app.db.jobs.get_job_by_id", return_value=job),
        patch("app.db.jobs.update_tailored_resume"),
        patch("app.db.jobs.update_tailored_resume_pdf", clear),
        patch("app.db.subscriptions.get_tier", return_value=tier),
        patch("app.db.profile.get_profile", return_value={"resume_url": f"{UID}/r.pdf"}),
        patch.object(resume_storage, "tailor_fingerprint", fp),
        patch.object(resume_storage, "remove_object", remove),
        patch("modules.ai_cover_letter.load_resume_text", return_value="base resume"),
        patch("modules.ai_resume_tailor.tailor_resume", tailor),
        patch.object(profile_router, "_store_tailored_pdf", store),
    ):
        out = profile_router._lazy_tailor_for_job(user, job)
    return out, tailor, store, clear, remove, fp


def test_a_current_tailoring_is_served_without_paying():
    out, tailor, store, *_ = _run(_job(pdf=CURRENT, text="OLD TEXT"))
    assert out == CURRENT
    tailor.assert_not_called()
    store.assert_not_called()


def test_a_stale_tailoring_is_redone_from_the_new_resume_not_rebuilt_from_old_text():
    out, tailor, store, *_ = _run(_job(pdf=STALE, text="TEXT FROM THE OLD RESUME"))
    assert out == CURRENT
    tailor.assert_called_once()
    store.assert_called_once_with(UID, "j1", "NEW TAILORED", NOW_FP, STALE)


def test_a_stale_tailoring_without_a_paid_tier_is_never_served():
    out, tailor, store, *_ = _run(_job(pdf=STALE, text="OLD TEXT"), tier="free")
    assert out is None
    tailor.assert_not_called()
    store.assert_not_called()


def test_text_without_a_pdf_still_rebuilds_the_pdf_only():
    out, tailor, store, *_ = _run(_job(pdf=None, text="PAID TEXT"))
    assert out == CURRENT
    tailor.assert_not_called()
    store.assert_called_once_with(UID, "j1", "PAID TEXT", NOW_FP)


def test_a_failed_pdf_after_a_stale_retailor_clears_and_removes_the_stale_file():
    """Else the next apply sees stale PDF + new text and pays to re-tailor again."""
    out, _, _, clear, remove, _ = _run(_job(pdf=STALE), store_error=RuntimeError("render"))
    assert out is None
    clear.assert_called_once_with("j1", None, UID)
    remove.assert_called_once_with(STALE)


def test_a_free_apply_with_no_tailoring_pays_nothing_for_the_fingerprint():
    out, *_, fp = _run(_job(), tier="free")
    assert out is None
    fp.assert_not_called()


def test_a_current_tailoring_survives_a_shortened_description():
    out, *_ = _run(_job(pdf=CURRENT, desc="thin"))
    assert out == CURRENT


def _best(lazy_result):
    user = type("U", (), {"id": UID, "email": "x@y.z"})()
    with (
        patch("app.db.jobs.get_by_link", return_value={"id": "j1"}) as by_link,
        patch.object(profile_router, "_lazy_tailor_for_job", return_value=lazy_result),
        patch.object(profile_router.profile_db, "get_profile", return_value={}),
        patch.object(resume_storage, "signed_url_from_path", side_effect=lambda p, u: f"S:{p}"),
        patch.object(resume_storage, "best_signed_url", return_value="S:base"),
    ):
        res = profile_router.resume_best_url(job_url="https://jobs/1", user=user)
    by_link.assert_called_once()  # no second job read
    return res


def test_best_serves_a_current_tailoring():
    assert _best(CURRENT) == {
        "url": f"S:{CURRENT}",
        "expires_in": resume_storage.SIGNED_URL_TTL_SECONDS,
        "type": "tailored",
    }


def test_best_sends_the_base_resume_when_no_current_tailoring():
    res = _best(None)
    assert res["url"] == "S:base" and res["type"] != "tailored"


def test_storing_a_new_tailoring_removes_the_stale_file():
    remove = MagicMock()
    with (
        patch("modules.ats_pdf_generator.structure_resume_data", return_value={}),
        patch("modules.ats_pdf_generator.generate_ats_pdf", return_value=b"%PDF-"),
        patch.object(resume_storage, "upload_job_tailored", return_value=CURRENT),
        patch("app.db.jobs.update_tailored_resume_pdf"),
        patch.object(resume_storage, "remove_object", remove),
    ):
        assert profile_router._store_tailored_pdf(UID, "j1", "TEXT", NOW_FP, STALE) == CURRENT
    remove.assert_called_once_with(STALE)
