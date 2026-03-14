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


def generate_cover_letter(job):
    if not ANTHROPIC_API_KEY:
        return fallback_template(job)

    resume_text = load_resume_text()
    profile = load_profile()

    prompt = f"""Write a professional, personalized cover letter for this job application.

Job Title: {job.get('title', '')}
Company: {job.get('company', '')}
Job Description: {job.get('description', 'Not available')}

Applicant Name: {profile.get('name', '')}
Applicant Email: {profile.get('email', '')}

Resume:
{resume_text if resume_text else 'No resume provided.'}

Instructions:
- Keep it concise (3-4 paragraphs)
- Be specific about how the applicant's experience matches the role
- Professional but not overly formal tone
- Do not fabricate skills or experience not in the resume"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except Exception as e:
        print(f"[cover_letter] AI generation failed: {e}")
        return fallback_template(job)
