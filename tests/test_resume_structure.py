"""The edited resume has to be the one that reaches the employer.

Before the structure was stored, a user who fixed a mistake in the generated resume
fixed it in the PDF only: tailoring, cover letters, screener answers, fit judging and
the interview kit all re-read the ORIGINAL upload. The tests that matter here are the
ones that pin that down — the authority order, and the fallbacks that must not take a
half-saved edit as the truth.
"""

from unittest.mock import patch

from modules.ai_cover_letter import MIN_STRUCTURE_TEXT, resume_text_for
from modules.ats_pdf_generator import sanitize_structure, structure_to_text

FULL = {
    "name": "Jane Roe",
    "title": "Registered Nurse",
    "contact": {
        "phone": "+1 808 555 0100",
        "email": "jane@example.com",
        "location": "Honolulu, HI",
        "linkedin": "",
    },
    "summary": "Critical care nurse with eight years in level-one trauma.",
    "competencies": ["Triage", "Charting", "Epic"],
    "experience": [
        {
            "title": "Charge Nurse",
            "company": "Queen's Medical Center",
            "location": "Honolulu, HI",
            "dates": "2020 – 2024",
            "bullets": ["Ran a 24-bed unit", "Cut handoff errors by a third"],
        }
    ],
    "education": [{"degree": "BSN", "school": "University of Hawaii", "year": "2016"}],
    "certifications": ["BLS", "ACLS"],
    "languages": ["English (native)"],
    "tech_skills": ["Epic", "Cerner"],
}


class TestSanitize:
    def test_drops_keys_the_renderers_do_not_know(self):
        out = sanitize_structure({**FULL, "injected": {"anything": True}})
        assert "injected" not in out

    def test_survives_wrong_types_instead_of_raising(self):
        # The editor posts what the user typed; a string where a list belongs must not
        # blow up inside reportlab, after the user already thinks the edit was saved.
        out = sanitize_structure({"name": 42, "competencies": "not a list", "experience": ["nope"]})
        assert out["name"] == ""
        assert out["competencies"] == []
        assert out["experience"] == []

    def test_keeps_a_job_that_only_has_bullets(self):
        out = sanitize_structure({"experience": [{"bullets": ["Did the work"]}]})
        assert out["experience"][0]["bullets"] == ["Did the work"]

    def test_drops_an_entirely_empty_row(self):
        out = sanitize_structure({"education": [{"degree": "", "school": "", "year": ""}]})
        assert out["education"] == []


class TestStructureToText:
    def test_prints_every_section_the_pdf_prints(self):
        text = structure_to_text(FULL)
        for expected in [
            "Jane Roe",
            "PROFESSIONAL SUMMARY",
            "CORE COMPETENCIES",
            "PROFESSIONAL EXPERIENCE",
            "- Ran a 24-bed unit",
            "EDUCATION & CERTIFICATIONS",
            "TECHNICAL SKILLS",
            "LANGUAGES",
        ]:
            assert expected in text

    def test_a_correction_appears_verbatim(self):
        # The whole point of editing the structure instead of re-prompting: what the
        # user typed is what comes out.
        fixed = {**FULL, "name": "Jane Roe-Smith"}
        assert "Jane Roe-Smith" in structure_to_text(fixed)

    def test_empty_structure_is_empty_not_a_crash(self):
        assert structure_to_text({}) == ""

    def test_a_credential_in_both_lists_is_written_once(self):
        """This text feeds tailoring and cover letters, and its contract is "the order
        the PDF prints it" — so it has to drop the duplicate the PDF drops."""
        data = {
            **FULL,
            "education": [
                {
                    "degree": "Google Project Management Certification",
                    "school": "Google",
                    "year": "2023",
                }
            ],
            "certifications": ["Google Project Management Certification (2023)"],
        }
        text = structure_to_text(data)
        assert text.count("Google Project Management Certification") == 1


class TestAuthority:
    def test_ats_dial_with_a_structure_reads_the_structure(self):
        profile = {"default_resume": "ats", "ats_structure": FULL, "resume_url": "u/resume.pdf"}
        with patch("modules.ai_cover_letter.load_resume_text") as pdf_read:
            text = resume_text_for(profile)
        pdf_read.assert_not_called()
        assert "Jane Roe" in text

    def test_original_dial_still_reads_the_upload(self):
        profile = {
            "default_resume": "original",
            "ats_structure": FULL,
            "resume_url": "u/resume.pdf",
        }
        with patch("modules.ai_cover_letter.load_resume_text", return_value="FROM PDF") as pdf_read:
            assert resume_text_for(profile) == "FROM PDF"
        pdf_read.assert_called_once()

    def test_legacy_ats_approved_counts_as_the_ats_dial(self):
        profile = {"ats_approved": True, "ats_structure": FULL, "resume_url": "u/resume.pdf"}
        with patch("modules.ai_cover_letter.load_resume_text") as pdf_read:
            assert "Jane Roe" in resume_text_for(profile)
        pdf_read.assert_not_called()

    def test_a_too_thin_structure_falls_back_to_the_pdf(self):
        # A half-saved edit must not become the resume we send.
        thin = {"name": "J", "summary": "Hi"}
        assert len(structure_to_text(thin)) < MIN_STRUCTURE_TEXT
        profile = {"default_resume": "ats", "ats_structure": thin, "resume_url": "u/resume.pdf"}
        with patch("modules.ai_cover_letter.load_resume_text", return_value="FROM PDF"):
            assert resume_text_for(profile) == "FROM PDF"

    def test_no_structure_at_all_falls_back(self):
        profile = {"default_resume": "ats", "resume_url": "u/resume.pdf"}
        with patch("modules.ai_cover_letter.load_resume_text", return_value="FROM PDF"):
            assert resume_text_for(profile) == "FROM PDF"

    def test_a_broken_structure_never_blocks_an_application(self):
        profile = {"default_resume": "ats", "ats_structure": FULL, "resume_url": "u/resume.pdf"}
        with patch("modules.ats_pdf_generator.structure_to_text", side_effect=RuntimeError("boom")):
            with patch("modules.ai_cover_letter.load_resume_text", return_value="FROM PDF"):
                assert resume_text_for(profile) == "FROM PDF"

    def test_max_chars_is_respected(self):
        profile = {"default_resume": "ats", "ats_structure": FULL}
        assert len(resume_text_for(profile, max_chars=50)) == 50


API = "/api/v1"


class TestStructureEndpoints:
    """The editor's own round-trip. The load-bearing assertion is the quota one:
    fixing a typo must not cost the user one of their daily AI generations."""

    def test_get_returns_null_for_a_resume_generated_before_we_kept_it(self, auth_client):
        with patch("app.db.profile.get_profile", return_value={"ats_resume_url": "u/ats.pdf"}):
            r = auth_client.get(f"{API}/profile/ats/structure")
        assert r.status_code == 200
        body = r.json()
        assert body["structure"] is None
        assert body["has_ats_resume"] is True

    def test_get_returns_the_stored_structure(self, auth_client):
        with patch("app.db.profile.get_profile", return_value={"ats_structure": FULL}):
            r = auth_client.get(f"{API}/profile/ats/structure")
        assert r.json()["structure"]["name"] == "Jane Roe"

    def test_save_rerenders_without_claude_and_without_spending_quota(self, auth_client):
        with (
            patch("app.routers.profile.generate_ats_pdf", return_value=b"%PDF") as pdf,
            patch("app.routers.profile.generate_ats_docx", return_value=b"DOCX"),
            patch("app.routers.profile.structure_resume_data") as claude,
            patch("app.db.usage.claim_daily_ai_slot") as quota,
            patch("app.db.resume.upload_ats", return_value="u/ats.pdf"),
            patch("app.db.resume.upload_ats_docx", return_value="u/ats.docx"),
            patch("app.db.resume.signed_download_url_ats", return_value="https://signed"),
            patch("app.routers.profile._seed_employment_from_resume"),
            patch("app.db.profile.update_ats") as saved,
        ):
            r = auth_client.put(f"{API}/profile/ats/structure", json={"structure": FULL})

        assert r.status_code == 200
        claude.assert_not_called()  # the user's words are not paraphrased on the way in
        quota.assert_not_called()  # and a correction is not billed as a generation
        assert pdf.call_args.kwargs["data"]["name"] == "Jane Roe"
        assert saved.call_args[0][1]["ats_structure"]["name"] == "Jane Roe"

    def test_save_clears_the_score_measured_on_the_old_render(self, auth_client):
        with (
            patch("app.routers.profile.generate_ats_pdf", return_value=b"%PDF"),
            patch("app.routers.profile.generate_ats_docx", return_value=b"DOCX"),
            patch("app.db.resume.upload_ats", return_value="u/ats.pdf"),
            patch("app.db.resume.upload_ats_docx", return_value="u/ats.docx"),
            patch("app.db.resume.signed_download_url_ats", return_value="https://signed"),
            patch("app.routers.profile._seed_employment_from_resume"),
            patch("app.db.profile.update_ats") as saved,
        ):
            auth_client.put(f"{API}/profile/ats/structure", json={"structure": FULL})

        payload = saved.call_args[0][1]
        assert payload["ats_score"] is None
        assert payload["ats_checked_at"] is None

    def test_a_nameless_resume_is_refused(self, auth_client):
        r = auth_client.put(
            f"{API}/profile/ats/structure", json={"structure": {**FULL, "name": ""}}
        )
        assert r.status_code == 400
        assert "Name" in r.json()["error"]

    def test_an_emptied_resume_is_refused(self, auth_client):
        bare = {"name": "Jane Roe", "experience": [], "summary": ""}
        r = auth_client.put(f"{API}/profile/ats/structure", json={"structure": bare})
        assert r.status_code == 400

    def test_injected_keys_never_reach_storage(self, auth_client):
        with (
            patch("app.routers.profile.generate_ats_pdf", return_value=b"%PDF"),
            patch("app.routers.profile.generate_ats_docx", return_value=b"DOCX"),
            patch("app.db.resume.upload_ats", return_value="u/ats.pdf"),
            patch("app.db.resume.upload_ats_docx", return_value="u/ats.docx"),
            patch("app.db.resume.signed_download_url_ats", return_value="https://signed"),
            patch("app.routers.profile._seed_employment_from_resume"),
            patch("app.db.profile.update_ats") as saved,
        ):
            auth_client.put(
                f"{API}/profile/ats/structure",
                json={"structure": {**FULL, "is_admin": True, "user_id": "someone-else"}},
            )

        stored = saved.call_args[0][1]["ats_structure"]
        assert "is_admin" not in stored
        assert "user_id" not in stored
