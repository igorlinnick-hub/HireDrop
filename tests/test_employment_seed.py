"""Building a resume never writes an ANSWER into the profile.

Until 10-06 every ATS build and every editor save copied the latest job title and
employer, city, state and zip from the structured resume into blank profile fields. Those
then counted as answered: the person never saw them, and the fillers put them on real
applications. The structure is a model's rewrite (its title is not necessarily the one
they held) and can belong to an older upload. What the resume says is now OFFERED in the
answers form (POST /profile/employer-answers/suggest) and written only when confirmed.

Zip stays, narrowly: written only under the city the person answered (the ZR contact step
needs one; a résumé zip under a different city would be a false address on a form).
"""

from unittest.mock import MagicMock, patch

from app.db import profile as profile_db

API = "/api/v1"
ANSWERS = {"current_title", "current_employer", "city", "state", "postal_code", "school", "degree"}
STRUCTURE = {
    "name": "Jane Roe",
    "contact": {"email": "jane@example.com", "location": "Miami, FL 33101"},
    "summary": "Analyst with six years of reporting and forecasting work.",
    "experience": [
        {"title": "Senior Analyst", "company": "Acme", "dates": "2021 – 2025", "bullets": ["x"]}
    ],
    "education": [{"school": "Coursera", "degree": "Google Data Analytics Certificate"}],
}


def _recording_supabase(written: list[dict]) -> MagicMock:
    client = MagicMock()

    def _update(payload):
        written.append(payload)
        return client.table.return_value.update.return_value

    client.table.return_value.update.side_effect = _update
    return client


def test_no_seeding_helpers_are_left_to_reuse():
    import app.routers.profile as router

    assert not hasattr(router, "_seed_employment_from_resume")
    for name in ("fill_current_employment_if_blank", "fill_address_if_blank"):
        assert not hasattr(profile_db, name)


def test_saving_the_edited_resume_writes_no_answer(auth_client):
    written: list[dict] = []
    with (
        patch.object(profile_db, "get_supabase", return_value=_recording_supabase(written)),
        patch.object(profile_db, "get_profile", return_value={}),  # every field blank
        patch("app.routers.profile.generate_ats_pdf", return_value=b"%PDF"),
        patch("app.routers.profile.generate_ats_docx", return_value=b"DOCX"),
        patch("app.db.resume.upload_ats", return_value="u/ats.pdf"),
        patch("app.db.resume.upload_ats_docx", return_value="u/ats.docx"),
        patch("app.db.resume.signed_download_url_ats", return_value="https://signed"),
    ):
        r = auth_client.put(f"{API}/profile/ats/structure", json={"structure": STRUCTURE})
    assert r.status_code == 200, r.text
    assert written, "the structure itself is still stored"
    assert not any(ANSWERS & set(w) for w in written)


def test_generating_from_text_writes_no_answer(auth_client):
    written: list[dict] = []
    with (
        patch.object(profile_db, "get_supabase", return_value=_recording_supabase(written)),
        patch.object(profile_db, "get_profile", return_value={}),  # every field blank
        patch("app.routers.profile.usage_db.claim_daily_ai_slot", return_value=True),
        patch("app.routers.profile.structure_resume_data", return_value=STRUCTURE),
        patch("app.routers.profile.generate_ats_pdf", return_value=b"%PDF"),
        patch("app.routers.profile.generate_ats_docx", return_value=b"DOCX"),
        patch("app.db.resume.upload_ats", return_value="u/ats.pdf"),
        patch("app.db.resume.upload_ats_docx", return_value="u/ats.docx"),
        patch("app.db.resume.signed_download_url_ats", return_value="https://signed"),
    ):
        r = auth_client.post(
            f"{API}/profile/ats/generate-from-text", json={"resume_text": "Jane Roe resume"}
        )
    assert r.status_code == 200, r.text
    assert written, "the structure itself is still stored"
    assert not any(ANSWERS & set(w) for w in written)


# The zip is the one exception: not an employer question, needed by the ZipRecruiter
# contact step — and written only under the city the person answered themselves.
def _put_structure(auth_client, stored: dict) -> list[dict]:
    written: list[dict] = []
    with (
        patch.object(profile_db, "get_supabase", return_value=_recording_supabase(written)),
        patch.object(profile_db, "get_profile", return_value=stored),
        patch("app.routers.profile.generate_ats_pdf", return_value=b"%PDF"),
        patch("app.routers.profile.generate_ats_docx", return_value=b"DOCX"),
        patch("app.db.resume.upload_ats", return_value="u/ats.pdf"),
        patch("app.db.resume.upload_ats_docx", return_value="u/ats.docx"),
        patch("app.db.resume.signed_download_url_ats", return_value="https://signed"),
    ):
        r = auth_client.put(f"{API}/profile/ats/structure", json={"structure": STRUCTURE})
    assert r.status_code == 200, r.text
    return written


def test_zip_seeded_under_the_persons_own_city(auth_client):
    written = _put_structure(auth_client, {"city": "miami", "postal_code": ""})
    assert {"postal_code": "33101"} in written
    assert not any((ANSWERS - {"postal_code"}) & set(w) for w in written)


def test_zip_not_seeded_under_another_city(auth_client):
    written = _put_structure(auth_client, {"city": "Austin", "postal_code": ""})
    assert not any("postal_code" in w for w in written)


def test_zip_never_overwrites_the_persons_own(auth_client):
    written = _put_structure(auth_client, {"city": "Miami", "postal_code": "33139"})
    assert not any("postal_code" in w for w in written)
