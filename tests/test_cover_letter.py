"""Tests for cover letter fallback — guards Necessary Condition C1.

Fallback is what every user gets when ANTHROPIC_API_KEY is missing or the
API call fails. If fallback contains AI-tells or hardcoded names, every
single application sent in fallback mode burns the user's reputation.
"""

from modules.ai_cover_letter import build_system_prompt, fallback_template, strip_preamble

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


def test_fallback_contains_no_dashes():
    """Em-dashes are the strongest AI-tell in generated text — the fallback
    letter must not contain them either."""
    job = {"title": "Marketing Lead", "company": "Acme Co", "description": ""}
    profile = {"name": "Anna", "last_name": "Smith"}
    letter = fallback_template(job, profile)
    assert "—" not in letter
    assert "--" not in letter


def test_system_prompt_bans_dashes():
    """The dash ban must survive prompt edits, with and without writing_style."""
    for style in ("", "Casual, short sentences."):
        prompt = build_system_prompt(style)
        assert "em-dash" in prompt.lower()
        assert "--" in prompt


# ---------------------------------------------------------------------------
# The letter must be ONLY the letter (2026-09-21)
#
# A model answering "write a cover letter" sometimes answers the request instead of just
# doing it — "Here's a cover letter for Jordan:" and then the letter inside --- fences.
# Nothing trimmed that: the router returns the text as-is and content.js types it into the
# employer's textarea. Measured on the production applications table that day: 4 of 90
# saved letters opened with "Here's a cover letter for <name>: ---", three of them from
# one user's September welding applications. Those reached real employers.
#
# The system prompt now forbids it; strip_preamble is the belt. These pin BOTH halves —
# and, just as importantly, that a normal letter comes through untouched.


def test_prompt_forbids_preamble_and_fences():
    prompt = build_system_prompt()
    assert "NOTHING else" in prompt
    assert "---" in prompt, "the fence ban must name the fence"


def test_strips_announcement_line_and_fences():
    raw = "Here's a cover letter for Jordan:\n\n---\n\nHi,\n\nI ran delivery at Northwind.\n\nJordan\n\n---"
    out = strip_preamble(raw)
    assert out.startswith("Hi,")
    assert out.endswith("Jordan")
    assert "Here's a cover letter" not in out
    assert "---" not in out


def test_strips_bare_fences_without_an_announcement():
    assert strip_preamble("---\nHi,\n\nBody here\n---") == "Hi,\n\nBody here"


def test_leaves_a_clean_letter_alone():
    letter = "Hi there,\n\nI'm a CWB-certified welder based in Mississauga.\n\nJedyn"
    assert strip_preamble(letter) == letter


def test_keeps_a_subject_line():
    # "Subject: …" is part of the letter, not an announcement about it.
    assert strip_preamble("Subject: Application\n\nHi,\n\nbody").startswith("Subject: Application")


def test_does_not_eat_a_sentence_that_merely_starts_with_here_is():
    # The announcement shape is a line ENDING in a colon. A real sentence that happens to
    # begin with "Here is" must survive — mangling a good letter is the worse failure.
    letter = "Here is what I did at Northwind: I ran delivery for 40 people.\n\nJordan"
    assert strip_preamble(letter) == letter


def test_empty_and_none_are_safe():
    assert strip_preamble("") == ""
    assert strip_preamble(None) is None


# ---------------------------------------------------------------------------
# One model writes every letter (2026-09-21, Igor)
#
# The old split gave tap the cheaper model on the reasoning that "the human reads + edits
# the letter before submit" — false since the 2026-07-25 rebuild, where the swipe happens
# before the letter exists. For two months the jobs a human picked got the WORSE letter.
# Measured worth of the split: $0.0037 per application, $3.36/month at the cap.
#
# This pins the decision itself, so a future "let's save a bit on weak-fit letters" has to
# argue with a failing test rather than quietly reintroduce the same class of bug.


def test_one_model_for_every_letter():
    from modules.ai_cover_letter import (
        COVER_LETTER_MODEL,
        COVER_LETTER_MODEL_AUTO,
        COVER_LETTER_MODEL_TAP,
    )

    assert COVER_LETTER_MODEL_TAP == COVER_LETTER_MODEL_AUTO == COVER_LETTER_MODEL, (
        "The letter model must not depend on submit mode. If you are re-introducing a "
        "split, read the comment above COVER_LETTER_MODEL first: the last one ran for two "
        "months on a reason that had stopped being true."
    )


def test_submit_mode_does_not_reach_the_model_choice():
    # Belt for the same rule at the call site: generate_cover_letter must not branch on
    # submit_mode any more. Source check, because the branch is what we're forbidding.
    import inspect

    from modules import ai_cover_letter

    src = inspect.getsource(ai_cover_letter.generate_cover_letter)
    assert "submit_mode" not in src, "generate_cover_letter must not read submit_mode"
