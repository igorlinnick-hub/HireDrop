"""Tests for cover letter fallback — guards Necessary Condition C1.

Fallback is what every user gets when ANTHROPIC_API_KEY is missing or the
API call fails. If fallback contains AI-tells or hardcoded names, every
single application sent in fallback mode burns the user's reputation.
"""
from modules.ai_cover_letter import fallback_template

# Tells the system prompt explicitly bans (modules/ai_cover_letter.py:53-54)
AI_TELLS = [
    "leverage",
    "passionate",
    "synergy",
    "excited to apply",
    "unique opportunity",
    "thrilled",
    "I am writing to express",
    "I hope this message finds you well",
]


def test_fallback_contains_no_ai_tells():
    job = {"title": "Marketing Lead", "company": "Acme Co", "description": ""}
    profile = {"name": "Anna", "last_name": "Smith"}
    letter = fallback_template(job, profile).lower()
    for tell in AI_TELLS:
        assert tell.lower() not in letter, f"fallback contains banned phrase: {tell!r}"


def test_fallback_no_hardcoded_author_name():
    """Old template hardcoded 'Igor Linnik' — every user signed his name."""
    job = {"title": "Marketing Lead", "company": "Acme Co"}
    profile = {"name": "Anna", "last_name": "Smith"}
    letter = fallback_template(job, profile)
    assert "Igor Linnik" not in letter
    assert "Igor" not in letter or "Anna" in letter  # only present if user is named Igor


def test_fallback_uses_user_name():
    job = {"title": "X", "company": "Y"}
    profile = {"name": "Anna", "last_name": "Smith"}
    letter = fallback_template(job, profile)
    assert "Anna Smith" in letter


def test_fallback_handles_missing_profile_name():
    job = {"title": "X", "company": "Y"}
    letter = fallback_template(job, {})
    assert "Applicant" in letter or letter.strip()  # graceful default


def test_fallback_handles_none_profile():
    job = {"title": "X", "company": "Y"}
    letter = fallback_template(job)
    assert letter.strip()  # doesn't crash


def test_fallback_substitutes_company_and_title():
    job = {"title": "Senior Engineer", "company": "Acme Co"}
    profile = {"name": "Anna"}
    letter = fallback_template(job, profile)
    assert "Acme Co" in letter
    assert "Senior Engineer" in letter


def test_fallback_handles_empty_company_gracefully():
    job = {"title": "Engineer", "company": ""}
    profile = {"name": "Anna"}
    letter = fallback_template(job, profile)
    # Should not produce literal "{company}" placeholder leak
    assert "{company}" not in letter
