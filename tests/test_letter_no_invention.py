"""Letters must not invent the employer, and must arrive as plain text (audit 09-27)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules import ai_cover_letter as cl

# Captured from a stored letter (applications, 09-21) — the header block and fence are real.
HEADER_LETTER = """**Igor Linnyk | igor.linnick@gmail.com | (713) 835-1446**

---

At a wellness clinic in Oahu, I built a full patient acquisition system from scratch.

That kind of **ground-level** outreach work is exactly what this role needs."""


def test_contact_header_and_markdown_are_removed():
    out = cl.to_plain_letter(HEADER_LETTER)
    assert out.startswith("At a wellness clinic in Oahu")
    assert "**" not in out and "---" not in out and "@" not in out
    assert "ground-level outreach" in out


def test_plain_letter_is_left_alone():
    letter = "Hi team,\n\nI ran the clinic's outreach end to end.\n\nIgor"
    assert cl.to_plain_letter(letter) == letter


def test_signature_with_contact_at_the_end_is_kept():
    letter = "Hi team,\n\nI ran outreach.\n\nIgor Linnyk\nigor@x.com | +1 (713) 835-1446"
    assert cl.to_plain_letter(letter) == letter


def _prompt_for(description):
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(text="Hi.")])
    with (
        patch.object(cl, "ANTHROPIC_API_KEY", "k"),
        patch.object(cl, "get_anthropic_client", return_value=client),
        patch.object(cl, "resume_text_for", return_value="Marketing, clinic."),
    ):
        cl.generate_cover_letter(
            {"title": "Event Coordinator", "company": "Sweet Jollof", "description": description},
            {},
        )
    kw = client.messages.create.call_args.kwargs
    return kw["messages"][0]["content"], kw["system"]


def test_thin_posting_is_labelled_so_the_company_is_not_described():
    # The real row: an Indeed posting whose "description" was only its salary chip.
    prompt, system = _prompt_for("$300 - $500 a week")
    assert "Do not describe the company" in prompt
    assert "outside knowledge" in system


def test_posting_window_is_1500_chars():
    prompt, _ = _prompt_for("y" * 5000)
    assert "y" * 1500 in prompt and "y" * 1501 not in prompt
