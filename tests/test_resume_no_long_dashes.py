"""No long dashes in the resume an employer reads.

Dashes reach a resume from three places: the layout separators, the date fields and the
structuring model's summary and bullets. Each renderer is run end to end and read back by
text extraction, the way an ATS reads it, so a new separator or field cannot slip by.
"""

import io

import pdfplumber
from docx import Document

from modules.ats_pdf_generator import generate_ats_docx, generate_ats_pdf
from modules.skills_resume import generate_skills_docx, generate_skills_pdf
from modules.text_style import has_long_dash, resume_no_long_dashes

DATA = {
    "name": "Igor Linnik",
    "title": "Growth Marketer — Paid Social",
    "summary": "I scale paid social — Meta and TikTok — for DTC brands.",
    "competencies": ["Paid Social", "A/B Testing — CRO"],
    "skill_groups": [{"name": "Ads", "skills": ["Meta Ads", "TikTok — Spark Ads"]}],
    "experience": [
        {
            "title": "Growth Lead",
            "company": "Acme",
            "location": "Honolulu, HI",
            "dates": "Jan 2020 – Present",
            "bullets": ["Grew revenue 3x — mostly lifecycle.", "Ran 40 tests -- 12 shipped."],
        }
    ],
    "education": [{"degree": "BA Economics", "school": "UH", "year": "2014–2018"}],
    "certifications": ["Meta Blueprint — Media Buying"],
    "tech_skills": ["Figma", "SQL"],
    "languages": ["English", "Russian"],
}


def _pdf_text(blob: bytes) -> str:
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


def _docx_text(blob: bytes) -> str:
    return "\n".join(p.text for p in Document(io.BytesIO(blob)).paragraphs)


def test_cleaner_keeps_dates_as_ranges():
    out = resume_no_long_dashes(DATA)
    job = out["experience"][0]
    assert job["dates"] == "Jan 2020 - Present"
    assert out["education"][0]["year"] == "2014-2018"
    assert job["bullets"][0] == "Grew revenue 3x, mostly lifecycle."
    assert DATA["experience"][0]["dates"] == "Jan 2020 – Present"  # input untouched


def test_every_rendered_resume_has_no_long_dash():
    for name, text in (
        ("ats pdf", _pdf_text(generate_ats_pdf(data=DATA))),
        ("ats docx", _docx_text(generate_ats_docx(data=DATA))),
        ("skills pdf", _pdf_text(generate_skills_pdf(DATA))),
        ("skills docx", _docx_text(generate_skills_docx(DATA))),
    ):
        assert not has_long_dash(text), f"{name}: {text!r}"
        assert "Jan 2020 - Present" in text, name
