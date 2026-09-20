"""Skills-style (second) resume: the render story and the default-resume dial.

The dial test is the one that matters: best_signed_url now serves the apply
path with THREE possible bases (original | ats | skills), and default_resume
must be the single authority when set — ats_approved is only the legacy
fallback (two live authorities on one decision is the fit-mode bug all over
again). Storage is monkeypatched; no network.
"""

from reportlab.platypus import Paragraph

from app.db import resume as resume_storage
from modules.skills_resume import build_skills_story

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
    # newlines, bullets, semicolons and a trailing "and" are all separators people use
    assert count_skill_items("- React\n- Node.js\n- SQL") == 3
    assert count_skill_items("Excel; PowerPoint; Meta Ads and Google Ads") == 4
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
