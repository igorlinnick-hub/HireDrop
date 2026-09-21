"""Skills-style (second) resume: the render story and the default-resume dial.

The dial test is the one that matters: best_signed_url now serves the apply
path with THREE possible bases (original | ats | skills), and default_resume
must be the single authority when set — ats_approved is only the legacy
fallback (two live authorities on one decision is the fit-mode bug all over
again). Storage is monkeypatched; no network.
"""

from reportlab.platypus import Paragraph

from app.db import resume as resume_storage
from modules.skills_resume import build_skills_story, dedupe_skill_groups

SAMPLE = {
    "name": "Jane Doe",
    "title": "Growth Marketer",
    "contact": {"email": "jane@x.com", "location": "Austin, TX", "phone": "", "linkedin": ""},
    "summary": "Marketer with automation focus.",
    "skill_groups": [
        {"group": "Marketing Automation", "skills": ["HubSpot", "Zapier"]},
        {"group": "Analytics", "skills": ["GA4", "Looker"]},
        {"group": "Empty Group", "skills": []},
    ],
    "experience": [
        {
            "title": "Marketing Lead",
            "company": "Acme",
            "dates": "2022 – 2024",
            "one_liner": "Owned lifecycle marketing; grew MQLs 3x.",
            "skills_gained": ["HubSpot", "GA4"],
        }
    ],
    "education": [{"degree": "BA Marketing", "school": "UT Austin", "year": "2020"}],
    "certifications": [],
    "languages": ["English (Native)"],
}


def _texts(story):
    return [f.text for f in story if isinstance(f, Paragraph)]


def test_skills_story_leads_with_grouped_skills():
    texts = _texts(build_skills_story(SAMPLE))
    joined = "\n".join(texts)
    skills_idx = next(i for i, t in enumerate(texts) if t == "SKILLS")
    exp_idx = next(i for i, t in enumerate(texts) if t == "EXPERIENCE")
    assert skills_idx < exp_idx, "skills must come before experience — that's the style"
    assert any("Marketing Automation" in t and "HubSpot | Zapier" in t for t in texts)
    # A group with no skills renders nothing (no orphan header line)
    assert "Empty Group" not in joined


def test_skills_story_experience_is_compact():
    texts = _texts(build_skills_story(SAMPLE))
    assert any("Marketing Lead" in t and "Acme" in t and "2022 – 2024" in t for t in texts)
    assert "Owned lifecycle marketing; grew MQLs 3x." in texts
    assert any("Skills gained:" in t and "HubSpot, GA4" in t for t in texts)


def test_certification_named_by_education_is_not_printed_twice():
    """Live run 2026-09-21: "Google Project Management Certification" printed on two
    consecutive lines of the same section — the model had put it in BOTH arrays."""
    from modules.ats_pdf_generator import dedupe_certifications

    edu = [
        {"degree": "B.S. in Economics", "school": "Kharkiv National University", "year": ""},
        {"degree": "Google Project Management Certification", "school": "Google", "year": "2023"},
    ]
    certs = ["Google Project Management Certification (2023)", "HubSpot Inbound (2021)"]
    assert dedupe_certifications(edu, certs) == ["HubSpot Inbound (2021)"]

    # Short degrees don't swallow unrelated credentials, and nothing is lost when
    # the two sections genuinely name different things.
    assert dedupe_certifications([{"degree": "MBA"}], ["MBA Certification"]) == [
        "MBA Certification"
    ]
    assert dedupe_certifications([], certs) == certs
    assert dedupe_certifications(edu, []) == []


def test_skills_story_prints_each_credential_once():
    data = dict(SAMPLE)
    data["education"] = [
        {"degree": "Google Project Management Certification", "school": "Google", "year": "2023"}
    ]
    data["certifications"] = ["Google Project Management Certification (2023)"]
    texts = _texts(build_skills_story(data))
    hits = [t for t in texts if "Google Project Management Certification" in t]
    assert len(hits) == 1, f"credential rendered {len(hits)} times: {hits}"


# ---------- the dial: best_signed_url(user, ats_approved, default_resume) ----------


def _patch_urls(monkeypatch, *, original="URL-orig", ats="URL-ats", skills="URL-skills"):
    monkeypatch.setattr(resume_storage, "signed_download_url", lambda uid: original)
    monkeypatch.setattr(resume_storage, "signed_download_url_ats", lambda uid: ats)
    monkeypatch.setattr(resume_storage, "signed_download_url_skills", lambda uid: skills)


def test_dial_unset_keeps_legacy_behavior(monkeypatch):
    _patch_urls(monkeypatch)
    assert resume_storage.best_signed_url("u", False) == "URL-orig"
    assert resume_storage.best_signed_url("u", True) == "URL-ats"


def test_dial_overrides_ats_approved(monkeypatch):
    _patch_urls(monkeypatch)
    # skills wins even though ats_approved is True — one authority
    assert resume_storage.best_signed_url("u", True, default_resume="skills") == "URL-skills"
    # explicit original wins over ats_approved=True
    assert resume_storage.best_signed_url("u", True, default_resume="original") == "URL-orig"
    assert resume_storage.best_signed_url("u", False, default_resume="ats") == "URL-ats"


# ---------- /profile/skills/describe: the user's words persist to the profile ----------


def test_count_skill_items_counts_what_people_actually_write():
    from modules.skills_resume import count_skill_items

    assert count_skill_items("") == 0
    assert count_skill_items("React, TypeScript, Figma") == 3
    # newlines, bullets and semicolons are all separators people use
    assert count_skill_items("- React\n- Node.js\n- SQL") == 3
    # "and" separates only when nothing else does
    assert count_skill_items("excel and word and photoshop") == 3
    # ...and is part of the NAME once the list has a real separator: Igor's live run
    # counted 12 comma-separated skills as 13 by splitting "Instagram Growth and
    # Automation". Over-counting walks someone past the 10-skill bar with 9.
    assert count_skill_items("Excel; PowerPoint; Meta Ads and Google Ads") == 3
    assert count_skill_items("Content Strategy, Instagram Growth and Automation") == 2
    # fragments too short to be a skill don't inflate the count
    assert count_skill_items("React, , a, Figma") == 2


def test_skills_describe_reports_progress_toward_minimum(auth_client):
    from unittest.mock import patch

    from modules.skills_resume import MIN_SKILLS

    with patch("app.routers.profile.profile_db.update_skills_resume"):
        res = auth_client.post(
            "/api/v1/profile/skills/describe", json={"description": "React, Figma, SQL"}
        )
    body = res.json()
    assert body["skill_count"] == 3
    assert body["min_skills"] == MIN_SKILLS
    assert body["meets_minimum"] is False
    # Saving is never blocked on the count — a half-written list must survive.
    assert body["saved"] is True


def test_skills_describe_saves_trimmed_capped(auth_client):
    from unittest.mock import patch

    with patch("app.routers.profile.profile_db.update_skills_resume") as upd:
        res = auth_client.post(
            "/api/v1/profile/skills/describe", json={"description": "  React, Figma  " + "x" * 5000}
        )
    assert res.status_code == 200
    saved = upd.call_args[0][1]["skills_description"]
    assert saved.startswith("React, Figma")
    assert len(saved) <= 4000
    assert res.json()["saved"] is True


def test_skills_describe_empty_clears(auth_client):
    from unittest.mock import patch

    with patch("app.routers.profile.profile_db.update_skills_resume") as upd:
        res = auth_client.post("/api/v1/profile/skills/describe", json={"description": "   "})
    assert res.status_code == 200
    assert upd.call_args[0][1]["skills_description"] == ""


def test_dial_falls_back_to_original_when_file_missing(monkeypatch):
    _patch_urls(monkeypatch, skills=None, ats=None)
    # chosen file missing must not 404 an apply in progress
    assert resume_storage.best_signed_url("u", False, default_resume="skills") == "URL-orig"
    assert resume_storage.best_signed_url("u", True, default_resume="ats") == "URL-orig"


class TestDuplicateSkills:
    """Igor, 2026-09-21: after adding skills, the skills resume prints some of them twice.

    The prompt already tells the model "EVERY skill they wrote must appear exactly
    once", and within one group it mostly complies. What it does not catch is the same
    skill landing in TWO groups — which is the repetition that reaches the page. An
    instruction to a model is not a guarantee, so the guarantee is code.
    """

    def test_the_same_skill_in_two_groups_prints_once(self):
        out = dedupe_skill_groups(
            {
                "skill_groups": [
                    {"group": "Data & Analytics", "skills": ["Excel", "SQL"]},
                    {"group": "Reporting", "skills": ["Excel", "Power BI"]},
                ]
            }
        )
        assert out["skill_groups"][0]["skills"] == ["Excel", "SQL"]
        assert out["skill_groups"][1]["skills"] == ["Power BI"]

    def test_casing_and_spacing_are_the_same_skill(self):
        out = dedupe_skill_groups(
            {"skill_groups": [{"group": "G", "skills": ["Excel", "excel", "EXCEL "]}]}
        )
        assert out["skill_groups"][0]["skills"] == ["Excel"]

    def test_the_first_spelling_is_the_one_kept(self):
        out = dedupe_skill_groups(
            {"skill_groups": [{"group": "G", "skills": ["Power BI", "power bi"]}]}
        )
        assert out["skill_groups"][0]["skills"] == ["Power BI"]

    def test_a_group_emptied_by_dedup_is_dropped(self):
        # A heading with nothing under it reads as a bug of its own.
        out = dedupe_skill_groups(
            {
                "skill_groups": [
                    {"group": "Keeps", "skills": ["SQL"]},
                    {"group": "Emptied", "skills": ["sql"]},
                ]
            }
        )
        assert [g["group"] for g in out["skill_groups"]] == ["Keeps"]

    def test_certifications_and_languages_get_the_same_pass(self):
        out = dedupe_skill_groups(
            {"certifications": ["BLS", "bls"], "languages": ["English", "english"]}
        )
        assert out["certifications"] == ["BLS"]
        assert out["languages"] == ["English"]

    def test_skills_gained_still_repeats_a_skill_listed_above(self):
        # Deliberate: naming the skill under the job that built it is the section's point.
        out = dedupe_skill_groups(
            {
                "skill_groups": [{"group": "G", "skills": ["Excel"]}],
                "experience": [{"title": "Analyst", "skills_gained": ["Excel", "Excel", "SQL"]}],
            }
        )
        assert out["experience"][0]["skills_gained"] == ["Excel", "SQL"]

    def test_survives_a_malformed_group(self):
        out = dedupe_skill_groups(
            {"skill_groups": ["not a dict", {"group": "G", "skills": ["SQL"]}]}
        )
        assert out["skill_groups"] == [{"group": "G", "skills": ["SQL"]}]

    def test_a_resume_without_skills_is_untouched(self):
        assert dedupe_skill_groups({"name": "Jane"}) == {"name": "Jane"}
