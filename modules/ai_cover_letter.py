import os
import json
import anthropic
from config import ANTHROPIC_API_KEY

RESUME_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "resume.pdf")
PROFILE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "profile.json")
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "templates", "cover_letter.txt")


def load_resume_text():
    if not os.path.exists(RESUME_PATH):
        return ""
    try:
        import pdfplumber
        with pdfplumber.open(RESUME_PATH) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception:
        return ""


def load_profile():
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, "r") as f:
            return json.load(f)
    return {}


def fallback_template(job):
    try:
        with open(TEMPLATE_PATH, "r") as f:
            template = f.read()
    except FileNotFoundError:
        return ""
    letter = template.replace("{company}", job.get("company", ""))
    letter = letter.replace("{title}", job.get("title", ""))
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
    if not ANTHROPIC_API_KEY:
        return fallback_template(job)

    if profile is None:
        profile = load_profile()

    resume_text = load_resume_text()
    writing_style = profile.get("writing_style", "")
    system = build_system_prompt(writing_style)

    prompt = f"""Write a cover letter for this job application.

Job Title: {job.get('title', '')}
Company: {job.get('company', '')}
Job Description: {job.get('description', 'Not available')}

Applicant Name: {profile.get('name', '')}
Applicant Email: {profile.get('email', '')}

Resume:
{resume_text if resume_text else 'No resume provided.'}"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except Exception as e:
        print(f"[cover_letter] AI generation failed: {e}")
        return fallback_template(job)
