import json
import os

import anthropic

from config import ANTHROPIC_API_KEY

RESUME_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "resume.pdf")
PROFILE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "profile.json")
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "templates", "cover_letter.txt")

_anthropic_client: anthropic.Anthropic | None = None


def get_anthropic_client() -> anthropic.Anthropic:
    """Module-level singleton — created once per process, reused across requests."""
    global _anthropic_client
    if _anthropic_client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


def load_resume_text(max_chars=2000):
    if not os.path.exists(RESUME_PATH):
        return ""
    try:
        import pdfplumber

        with pdfplumber.open(RESUME_PATH) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            return text[:max_chars]
    except Exception:
        return ""


def load_profile():
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH) as f:
            return json.load(f)
    return {}


def fallback_template(job, profile=None):
    try:
        with open(TEMPLATE_PATH) as f:
            template = f.read()
    except FileNotFoundError:
        return ""
    profile = profile or {}
    name_parts = [profile.get("name", "").strip(), profile.get("last_name", "").strip()]
    name = " ".join(p for p in name_parts if p) or "Applicant"
    letter = template.replace("{company}", job.get("company", "") or "the team")
    letter = letter.replace("{title}", job.get("title", "") or "the role")
    letter = letter.replace("{name}", name)
    return letter


def build_system_prompt(writing_style=""):
    style_instruction = ""
    if writing_style:
        style_instruction = f"""
WRITING STYLE TO MATCH:
\"\"\"{writing_style}\"\"\"
Match this person's tone, vocabulary, and rhythm exactly.
"""
    return f"""You write job application cover letters for real humans.

STRICT RULES:
- Sound like the person wrote it themselves, NOT like an AI assistant
- NO buzzwords: leverage, passionate, synergy, excited to apply, unique opportunity, thrilled
- NO formal openers like "I am writing to express my interest" or "I hope this message finds you well"
- Short paragraphs. Max 2-3 sentences each.
- Natural rhythm. Occasional imperfection is fine and actually good.
- Max 120 words total. Be concise.
- Be direct: what you did, why this job, one specific thing that interests you about the company.
- Do NOT list your skills like a resume. Tell a micro-story instead.
{style_instruction}"""


def generate_cover_letter(job, profile=None):
    if profile is None:
        profile = load_profile()

    if not ANTHROPIC_API_KEY:
        return fallback_template(job, profile)

    resume_text = load_resume_text()
    writing_style = profile.get("writing_style", "")
    system = build_system_prompt(writing_style)

    description = job.get("description", "Not available")[:500]

    prompt = f"""Write a cover letter for this job application.

Job Title: {job.get("title", "")}
Company: {job.get("company", "")}
Job Description: {description}

Applicant Name: {profile.get("name", "")}
Applicant Email: {profile.get("email", "")}

Candidate background (from resume):
{resume_text if resume_text else "Not provided."}"""

    try:
        client = get_anthropic_client()
        message = client.messages.create(
            # Sonnet 4 (claude-sonnet-4-20250514) reaches end-of-life 2026-06-15.
            # 4.6 is the current default for cover-letter quality vs cost.
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except Exception as e:
        print(f"[cover_letter] AI generation failed: {e}")
        return fallback_template(job, profile)
