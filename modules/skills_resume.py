"""Skills-style resume — the user's SECOND resume, hybrid format.

Not a pure functional resume (recruiters read those as gap-hiding and some ATS
parse them poorly). Hybrid: grouped skills lead the page, each work entry is
compressed to a 1-2 line chronology plus "what this role leveled up". Same
ATS-safe formatting rules as ats_pdf_generator (Helvetica/Arial, no tables,
hyphen bullets, 0.75" margins).

Flow mirrors the ATS resume: one Claude call structures the resume text into
JSON (skill groups included), then reportlab/python-docx render both formats
from that single structuring.
"""

import io
import json
import re

from reportlab.lib.pagesizes import letter
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from config import ANTHROPIC_API_KEY
from modules.ats_pdf_generator import (
    _MARGIN,
    SONNET_MODEL,
    _docx_inch,
    _docx_section_header,
    _make_styles,
    _section_block,
    clean_linkedin_url,
    dedupe_certifications,
    esc,
    skill_key,
)

# The candidate writes their own skills; we ask for at least this many so the
# grouping has something real to work with. Not a hard backend gate — the UI asks,
# and a saved description shorter than this still generates rather than dead-ends.
MIN_SKILLS = 10


def count_skill_items(text: str) -> int:
    """How many distinct skills the candidate listed.

    They write freely, so count the separators people actually use — commas,
    semicolons, newlines and bullet marks — and ignore fragments too short to be
    a skill. This is the number the UI's "7 / 10" counter shows, so the rule lives
    here and is tested rather than guessed at in two places.

    "and" counts as a separator ONLY in a list that has no other one ("excel and
    word and photoshop"). Once someone has picked a separator, an "and" is part of
    a skill's NAME, not a break between two: the live run 2026-09-21 counted Igor's
    12 comma-separated skills as 13 by splitting "Instagram Growth and Automation".
    Over-counting is the harmful direction — it walks someone past the 10-skill bar
    with 9, and the bar exists so the grouping has something real to work with.
    """
    if not text:
        return 0
    pattern = r"[,;\n•·|]+"
    if not re.search(pattern, text):
        pattern += r"|(?<=[a-z])\s+and\s+(?=[A-Za-z])"
    parts = re.split(pattern, text)
    return len([p for p in (part.strip(" \t-–—.") for part in parts) if len(p) >= 2])


def dedupe_skill_groups(data: dict) -> dict:
    """Print every skill once, in the first group that claimed it.

    The prompt asks for this ("EVERY skill they wrote must appear exactly once") and
    the model mostly obliges WITHIN a group — what it does not catch is the same skill
    landing in two different groups, which is the duplication users actually see. An
    instruction is not a guarantee, so the guarantee lives here.

    Groups left empty by the pass are dropped: a heading with nothing under it reads
    as a bug of its own. Certifications, languages and each job's skills_gained get the
    same treatment; skills_gained is deliberately NOT deduped against the groups above
    it, because repeating a skill under the job that built it is the point of the section.
    """
    if not isinstance(data, dict):
        return data

    seen: set[str] = set()
    groups = []
    for grp in data.get("skill_groups") or []:
        if not isinstance(grp, dict):
            continue
        kept = []
        for skill in grp.get("skills") or []:
            key = skill_key(skill)
            if not key or key in seen:
                continue
            seen.add(key)
            kept.append(skill)
        if kept:
            groups.append({**grp, "skills": kept})
    if data.get("skill_groups") is not None:
        data["skill_groups"] = groups

    for field in ("certifications", "languages"):
        items = data.get(field)
        if isinstance(items, list):
            local: set[str] = set()
            out = []
            for item in items:
                key = skill_key(item)
                if not key or key in local:
                    continue
                local.add(key)
                out.append(item)
            data[field] = out

    for job in data.get("experience") or []:
        if not isinstance(job, dict) or not isinstance(job.get("skills_gained"), list):
            continue
        local = set()
        out = []
        for skill in job["skills_gained"]:
            key = skill_key(skill)
            if not key or key in local:
                continue
            local.add(key)
            out.append(skill)
        job["skills_gained"] = out

    return data


def structure_skills_resume(resume_text: str, answers: list[dict] | None = None) -> dict:
    """Structure resume text into the skills-resume JSON, once for both formats.

    The candidate's own description of their skills (passed in `answers`) is the
    AUTHORITY on the skills themselves: we fix grammar, spelling and casing and
    group them — we never invent, rename or drop a skill they wrote. The resume
    supplies the chronology (employers, dates, roles).
    """
    if not ANTHROPIC_API_KEY or not resume_text.strip():
        return {}

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        answers_block = ""
        if answers:
            qa_lines = "\n".join(
                f"Q: {a.get('question', '')}\nA: {a.get('answer', '')}"
                for a in answers
                if a.get("answer", "").strip()
            )
            if qa_lines:
                answers_block = f"""

SKILLS AS THE CANDIDATE WROTE THEM (this is the AUTHORITY on skills — see rules):
{qa_lines}"""

        prompt = f"""Extract the resume below into this exact JSON structure for a SKILLS-FIRST resume.
Return ONLY the JSON object, no explanation, no markdown.

Rules:
- "skill_groups": the skills are the CANDIDATE'S OWN WORDS, given below. Your job is
  copy-editing, not authoring:
  * fix grammar, spelling, capitalization and obvious typos (e.g. "exel" → "Excel",
    "comunication" → "Communication", "managed peoples" → "People Management");
  * keep the candidate's meaning and wording — do NOT rewrite a skill into different
    words, do NOT merge two of their skills into one, do NOT invent a qualifier;
  * they often write in prose, not in a list ("working with code, security, backend
    and frontend; video editing and writing scripts for the medical niche"). SEPARATE
    that into one entry per distinct skill, using THEIR words for each: "Code",
    "Security", "Backend", "Frontend", "Video Editing", "Scriptwriting", "Medical
    Compliance". A skills section is scanned by keyword — an entry longer than about
    five words is a sentence, not a skill, and matches nothing;
  * drop the padding around a skill, never the skill: "development of automations at
    a professional level" → "Automation Development"; "P&L calculation and other
    metrics" → "P&L", "Financial Metrics". Context that is a CREDENTIAL, not padding
    (a named client, a named platform), belongs in the experience section, not inside
    the skill entry;
  * write every entry in the language of the resume text, even when they wrote their
    skills in another language — this resume is read by employers in the resume's
    market. Translate plainly: a translated skill keeps its meaning, gains nothing;
  * EVERY skill they wrote must appear, none dropped, none duplicated;
  * do NOT add skills they did not write, not even ones implied by the resume;
  * then sort them into 3-6 named groups a recruiter would search for (e.g.
    "Marketing Automation", "Team Leadership", "Data & Analytics").
  If they wrote NO skills of their own, and only then, take the skills from the resume.
- "experience": COMPACT. Per job exactly one "one_liner" (max ~18 words: what the
  role was about + the headline result) and "skills_gained" (2-4 skills this role
  leveled up — each must be one of the skills above, worded identically). NO bullets.
- Do NOT invent employers, dates, or results that are not in the resume text.

{{
  "name": "FULL NAME",
  "title": "Professional title / tagline (if present, else empty string)",
  "contact": {{"phone": "", "email": "", "location": "", "linkedin": ""}},
  "summary": "2-3 sentence positioning paragraph focused on capabilities",
  "skill_groups": [
    {{"group": "Group Name", "skills": ["Skill 1", "Skill 2"]}}
  ],
  "experience": [
    {{
      "title": "Job Title",
      "company": "Company Name",
      "dates": "Month Year – Month Year",
      "one_liner": "One compact line: scope + headline result",
      "skills_gained": ["Skill 1", "Skill 2"]
    }}
  ],
  "education": [{{"degree": "Degree Name", "school": "School Name", "year": ""}}],
  "certifications": ["Certification 1"],
  "languages": ["Language (Level)"]
}}

RESUME TEXT:
{resume_text[:4000]}{answers_block}"""

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
        data = json.loads(raw)
        contact = data.get("contact") or {}
        if contact.get("linkedin"):
            contact["linkedin"] = clean_linkedin_url(contact["linkedin"])
            data["contact"] = contact
        if not data.get("name"):
            lines = [ln.strip() for ln in resume_text.split("\n") if ln.strip()]
            data["name"] = lines[0] if lines else "CANDIDATE"
        return dedupe_skill_groups(data)
    except Exception as e:
        print(f"[skills_resume] Structure extraction failed: {e}")
        return {}


def build_skills_story(data: dict, styles: dict | None = None) -> list:
    """Flowables for the skills-first layout: header → summary → SKILLS (lead)
    → compact experience → education/certs/languages. Pure — testable."""
    styles = styles or _make_styles()
    story = []

    story.append(Paragraph(esc((data.get("name") or "").upper()), styles["name"]))
    if data.get("title"):
        story.append(Paragraph(esc(data["title"]), styles["title_line"]))
    c = data.get("contact") or {}
    contact_parts = [
        esc(p) for p in [c.get("phone"), c.get("email"), c.get("location"), c.get("linkedin")] if p
    ]
    if contact_parts:
        story.append(Paragraph(" | ".join(contact_parts), styles["contact"]))
    story.append(Spacer(1, 4))

    if data.get("summary"):
        story.extend(_section_block("Summary", styles))
        story.append(Paragraph(esc(data["summary"]), styles["body"]))

    # Skills lead the page — that is the point of this resume style.
    groups = data.get("skill_groups") or []
    if groups:
        story.extend(_section_block("Skills", styles))
        for grp in groups:
            name = esc(grp.get("group") or "")
            skills = [esc(s) for s in (grp.get("skills") or []) if s]
            if not skills:
                continue
            story.append(
                Paragraph(f"<b>{name}:</b>  {' | '.join(skills)}", styles["competency_row"])
            )

    exp = data.get("experience") or []
    if exp:
        story.extend(_section_block("Experience", styles))
        for job in exp:
            meta = [esc(p) for p in [job.get("company"), job.get("dates")] if p]
            header = f"<b>{esc(job.get('title') or '')}</b>"
            if meta:
                header += f"  |  {' — '.join(meta)}"
            story.append(Paragraph(header, styles["job_title"]))
            if job.get("one_liner"):
                story.append(Paragraph(esc(job["one_liner"]), styles["job_meta"]))
            gained = [esc(s) for s in (job.get("skills_gained") or []) if s]
            if gained:
                story.append(
                    Paragraph(f"<i>Skills gained:</i> {', '.join(gained)}", styles["job_meta"])
                )
            story.append(Spacer(1, 3))

    edu = data.get("education") or []
    certs = dedupe_certifications(edu, data.get("certifications") or [])
    if edu or certs:
        story.extend(_section_block("Education & Certifications", styles))
        for e in edu:
            parts = [esc(p) for p in [e.get("school"), e.get("year")] if p]
            line = f"<b>{esc(e.get('degree') or '')}</b>"
            if parts:
                line += f" — {' | '.join(parts)}"
            story.append(Paragraph(line, styles["body"]))
        for cert in certs:
            story.append(Paragraph(f"<b>{esc(cert)}</b>", styles["body"]))

    langs = data.get("languages") or []
    if langs:
        story.extend(_section_block("Languages", styles))
        story.append(Paragraph(" | ".join(esc(x) for x in langs), styles["body"]))

    return story


def generate_skills_pdf(data: dict) -> bytes:
    """Render the structured skills-resume data as an ATS-safe PDF."""
    story = build_skills_story(data)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=_MARGIN,
        bottomMargin=_MARGIN,
        title=f"{data.get('name', 'Resume')} — Skills Resume",
    )
    doc.build(story)
    return buffer.getvalue()


def generate_skills_docx(data: dict) -> bytes:
    """Render the same structured data as .docx (for Word-only boards)."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(10.5)
    for section in doc.sections:
        section.top_margin = section.bottom_margin = _docx_inch(0.75)
        section.left_margin = section.right_margin = _docx_inch(0.75)

    def add_line(text, *, size=10.5, bold=False, italic=False, space_after=0, space_before=0):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        p.paragraph_format.space_after = Pt(space_after)
        p.paragraph_format.space_before = Pt(space_before)
        r = p.add_run(text)
        r.bold = bold
        r.italic = italic
        r.font.size = Pt(size)
        r.font.name = "Arial"
        return p

    add_line((data.get("name") or "").upper(), size=16, bold=True, space_after=2)
    if data.get("title"):
        add_line(data["title"], space_after=1)
    c = data.get("contact") or {}
    contact_parts = [
        p for p in [c.get("phone"), c.get("email"), c.get("location"), c.get("linkedin")] if p
    ]
    if contact_parts:
        add_line(" | ".join(contact_parts))

    if data.get("summary"):
        _docx_section_header(doc, "Summary")
        add_line(data["summary"], space_after=2)

    groups = data.get("skill_groups") or []
    if groups:
        _docx_section_header(doc, "Skills")
        for grp in groups:
            skills = [s for s in (grp.get("skills") or []) if s]
            if not skills:
                continue
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(0)
            r = p.add_run(f"{grp.get('group') or ''}:  ")
            r.bold = True
            r.font.size = Pt(10.5)
            r.font.name = "Arial"
            r2 = p.add_run(" | ".join(skills))
            r2.font.size = Pt(10.5)
            r2.font.name = "Arial"

    exp = data.get("experience") or []
    if exp:
        _docx_section_header(doc, "Experience")
        for job in exp:
            meta = [p for p in [job.get("company"), job.get("dates")] if p]
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(3)
            p.paragraph_format.space_after = Pt(0)
            r = p.add_run(job.get("title") or "")
            r.bold = True
            r.font.size = Pt(11)
            r.font.name = "Arial"
            if meta:
                r2 = p.add_run("  |  " + " — ".join(meta))
                r2.font.size = Pt(10.5)
                r2.font.name = "Arial"
            if job.get("one_liner"):
                add_line(job["one_liner"])
            gained = [s for s in (job.get("skills_gained") or []) if s]
            if gained:
                add_line(f"Skills gained: {', '.join(gained)}", italic=True)

    edu = data.get("education") or []
    certs = dedupe_certifications(edu, data.get("certifications") or [])
    if edu or certs:
        _docx_section_header(doc, "Education & Certifications")
        for e in edu:
            parts = [p for p in [e.get("school"), e.get("year")] if p]
            line = e.get("degree") or ""
            if parts:
                line += " — " + " | ".join(parts)
            add_line(line, bold=True)
        for cert in certs:
            add_line(cert, bold=True)

    langs = data.get("languages") or []
    if langs:
        _docx_section_header(doc, "Languages")
        add_line(" | ".join(langs))

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
