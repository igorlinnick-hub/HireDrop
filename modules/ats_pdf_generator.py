"""Generate ATS-compliant PDF resumes from raw resume text.

Format spec (per the ATS guide in the project):
  - Font: Helvetica (PDF-safe Arial equivalent)
  - Name: 16pt bold, ALL CAPS, left-aligned
  - Section headers: 12pt bold, left-aligned, with horizontal rule
  - Job title lines: 11pt bold, left-aligned
  - Body / bullets: 10.5pt, line spacing 1.0
  - Margins: 0.75 inch
  - Section spacing: 6pt between sections
  - Bullets: hyphen + space (no special symbols)
  - No tables, no images, no columns, no text boxes

Flow:
  1. Claude structures raw resume text into JSON
  2. reportlab renders structured JSON as a clean PDF
"""

import io
import json

import pdfplumber
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.lib import colors

from config import ANTHROPIC_API_KEY

SONNET_MODEL = "claude-sonnet-4-6"

_MARGIN = 0.75 * inch
_PAGE_WIDTH, _PAGE_HEIGHT = letter


def _extract_text_from_bytes(pdf_bytes: bytes) -> str:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        parts = []
        for page in pdf.pages:
            text = page.extract_text() or ""
            parts.append(text)
    return "\n".join(parts)


def _structure_resume(resume_text: str) -> dict:
    """Ask Claude to parse resume text into a structured JSON object."""
    if not ANTHROPIC_API_KEY:
        return {}

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        prompt = f"""Extract the resume below into this exact JSON structure.
Return ONLY the JSON object, no explanation, no markdown.

{{
  "name": "FULL NAME",
  "title": "Professional title / tagline (if present, else empty string)",
  "contact": {{
    "phone": "",
    "email": "",
    "location": "",
    "linkedin": ""
  }},
  "summary": "Professional summary paragraph (plain text, no bullets)",
  "competencies": ["Competency 1", "Competency 2"],
  "experience": [
    {{
      "title": "Job Title",
      "company": "Company Name",
      "location": "City, State",
      "dates": "Month Year – Month Year",
      "bullets": ["Bullet point 1", "Bullet point 2"]
    }}
  ],
  "education": [
    {{
      "degree": "Degree Name",
      "school": "School Name",
      "year": "Year (or empty)"
    }}
  ],
  "certifications": ["Certification 1"],
  "languages": ["Language (Level)", "Language (Level)"],
  "tech_skills": ["Skill 1", "Skill 2"]
}}

RESUME TEXT:
{resume_text[:4000]}"""

        message = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        print(f"[ats_pdf] Structure extraction failed: {e}")
        return {}


def _make_styles() -> dict:
    base = ParagraphStyle(
        "base",
        fontName="Helvetica",
        fontSize=10.5,
        leading=12.6,
        textColor=colors.black,
        alignment=TA_LEFT,
        spaceAfter=0,
    )
    return {
        "name": ParagraphStyle(
            "name", parent=base,
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=19,
            spaceAfter=2,
            textTransform="uppercase",
        ),
        "title_line": ParagraphStyle(
            "title_line", parent=base,
            fontSize=10.5,
            leading=13,
            spaceAfter=1,
        ),
        "contact": ParagraphStyle(
            "contact", parent=base,
            fontSize=10.5,
            leading=13,
            spaceAfter=0,
        ),
        "section_header": ParagraphStyle(
            "section_header", parent=base,
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=14,
            spaceBefore=6,
            spaceAfter=2,
        ),
        "job_title": ParagraphStyle(
            "job_title", parent=base,
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=13,
            spaceBefore=3,
            spaceAfter=0,
        ),
        "job_meta": ParagraphStyle(
            "job_meta", parent=base,
            fontSize=10.5,
            leading=12.6,
            spaceAfter=1,
        ),
        "bullet": ParagraphStyle(
            "bullet", parent=base,
            fontSize=10.5,
            leading=12.6,
            leftIndent=12,
            spaceAfter=0,
        ),
        "body": ParagraphStyle(
            "body", parent=base,
            fontSize=10.5,
            leading=12.6,
            spaceAfter=2,
        ),
        "competency_row": ParagraphStyle(
            "competency_row", parent=base,
            fontSize=10.5,
            leading=13,
            spaceAfter=0,
        ),
    }


def _section_block(title: str, styles: dict) -> list:
    return [
        Spacer(1, 4),
        Paragraph(title.upper(), styles["section_header"]),
        HRFlowable(width="100%", thickness=0.75, color=colors.black, spaceAfter=3),
    ]


def _build_story(data: dict, styles: dict) -> list:
    story = []

    # Name
    name = (data.get("name") or "").upper()
    story.append(Paragraph(name, styles["name"]))

    # Optional title line
    if data.get("title"):
        story.append(Paragraph(data["title"], styles["title_line"]))

    # Contact line
    c = data.get("contact") or {}
    contact_parts = [p for p in [
        c.get("phone"), c.get("email"), c.get("location"), c.get("linkedin")
    ] if p]
    if contact_parts:
        story.append(Paragraph(" | ".join(contact_parts), styles["contact"]))

    story.append(Spacer(1, 4))

    # Professional Summary
    if data.get("summary"):
        story.extend(_section_block("Professional Summary", styles))
        story.append(Paragraph(data["summary"], styles["body"]))

    # Core Competencies
    comps = data.get("competencies") or []
    if comps:
        story.extend(_section_block("Core Competencies", styles))
        # Render as pipe-separated rows, max ~4 per row
        chunk_size = 4
        chunks = [comps[i:i + chunk_size] for i in range(0, len(comps), chunk_size)]
        for chunk in chunks:
            story.append(Paragraph(" | ".join(chunk), styles["competency_row"]))

    # Professional Experience
    exp = data.get("experience") or []
    if exp:
        story.extend(_section_block("Professional Experience", styles))
        for job in exp:
            job_title = job.get("title") or ""
            company = job.get("company") or ""
            location = job.get("location") or ""
            dates = job.get("dates") or ""

            meta_parts = [p for p in [company, location, dates] if p]
            header_text = f"<b>{job_title}</b>"
            if meta_parts:
                header_text += f"  |  {' — '.join(meta_parts)}"
            story.append(Paragraph(header_text, styles["job_title"]))

            for bullet in job.get("bullets") or []:
                bullet_text = bullet.lstrip("-•– ").strip()
                story.append(Paragraph(f"- {bullet_text}", styles["bullet"]))
            story.append(Spacer(1, 3))

    # Education & Certifications
    edu = data.get("education") or []
    certs = data.get("certifications") or []
    if edu or certs:
        story.extend(_section_block("Education & Certifications", styles))
        for e in edu:
            degree = e.get("degree") or ""
            school = e.get("school") or ""
            year = e.get("year") or ""
            parts = [p for p in [school, year] if p]
            line = f"<b>{degree}</b>"
            if parts:
                line += f" — {' | '.join(parts)}"
            story.append(Paragraph(line, styles["body"]))
        for cert in certs:
            story.append(Paragraph(f"<b>{cert}</b>", styles["body"]))

    # Tech Skills
    tech = data.get("tech_skills") or []
    if tech:
        story.extend(_section_block("Technical Skills", styles))
        chunk_size = 4
        chunks = [tech[i:i + chunk_size] for i in range(0, len(tech), chunk_size)]
        for chunk in chunks:
            story.append(Paragraph(" | ".join(chunk), styles["competency_row"]))

    # Languages
    langs = data.get("languages") or []
    if langs:
        story.extend(_section_block("Languages", styles))
        story.append(Paragraph(" | ".join(langs), styles["body"]))

    return story


def generate_ats_pdf(pdf_bytes: bytes | None = None, resume_text: str | None = None) -> bytes:
    """Generate an ATS-compliant PDF.

    Pass either:
      - pdf_bytes: original uploaded PDF (text will be extracted)
      - resume_text: pre-extracted plain text

    Returns PDF bytes.
    """
    if pdf_bytes and not resume_text:
        resume_text = _extract_text_from_bytes(pdf_bytes)

    if not resume_text or not resume_text.strip():
        raise ValueError("No resume text to process")

    data = _structure_resume(resume_text)

    if not data.get("name"):
        lines = [l.strip() for l in resume_text.split("\n") if l.strip()]
        data["name"] = lines[0] if lines else "CANDIDATE"

    styles = _make_styles()
    story = _build_story(data, styles)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=_MARGIN,
        bottomMargin=_MARGIN,
        title=f"{data.get('name', 'Resume')} — ATS Resume",
    )
    doc.build(story)
    return buffer.getvalue()
