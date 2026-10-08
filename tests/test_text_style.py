"""No long dashes in text an employer reads (Igor's rule, 2026-10-08)."""

import pytest

from modules.ai_cover_letter import to_plain_letter
from modules.text_style import has_long_dash, no_long_dashes


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (
            "I ran paid social — Meta and TikTok — for two years.",
            "I ran paid social, Meta and TikTok, for two years.",
        ),
        ("Growth work—mostly lifecycle.", "Growth work, mostly lifecycle."),
        ("Built it -- then scaled it.", "Built it, then scaled it."),
        ("Launched in 2019–2021 at 10 – 20 stores.", "Launched in 2019-2021 at 10-20 stores."),
        ("That was the goal. — Next, I hired.", "That was the goal. Next, I hired."),
        ("Here is the short answer —", "Here is the short answer"),
        ("— Owned the funnel\n— Ran tests", "Owned the funnel\nRan tests"),
        (
            "Plain text, nothing to change - a hyphen stays.",
            "Plain text, nothing to change - a hyphen stays.",
        ),
        ("well-known e-commerce brand", "well-known e-commerce brand"),
        ("", ""),
    ],
)
def test_no_long_dashes(raw, clean):
    assert no_long_dashes(raw) == clean
    assert not has_long_dash(no_long_dashes(raw))


def test_letter_exit_has_no_long_dash():
    raw = "Hi Suno team,\n\nI build landing pages — and I test them.\n\n---\n\nIgor"
    out = to_plain_letter(raw)
    assert not has_long_dash(out)
    assert "landing pages, and I test them." in out


def test_answerer_free_text_is_cleaned(monkeypatch):
    from modules import ai_question_answer as qa

    class _Msg:
        content = [type("C", (), {"text": "Optimizely — mostly A/B tests — and Hotjar."})()]

    class _Client:
        class messages:  # noqa: N801 - mimics the SDK shape
            @staticmethod
            def create(**_kw):
                return _Msg()

    monkeypatch.setattr(qa, "get_anthropic_client", lambda: _Client())
    monkeypatch.setattr(qa, "resume_text_for", lambda *_a, **_k: "")
    out = qa.answer_screener_question("Which testing platforms have you used?", job={}, profile={})
    assert out == "Optimizely, mostly A/B tests, and Hotjar."


def test_answerer_option_is_returned_verbatim(monkeypatch):
    from modules import ai_question_answer as qa

    option = "Yes — on site"

    class _Msg:
        content = [type("C", (), {"text": option})()]

    class _Client:
        class messages:  # noqa: N801
            @staticmethod
            def create(**_kw):
                return _Msg()

    monkeypatch.setattr(qa, "get_anthropic_client", lambda: _Client())
    monkeypatch.setattr(qa, "resume_text_for", lambda *_a, **_k: "")
    out = qa.answer_screener_question(
        "Can you work on site?", job={}, profile={}, options=[option, "No"]
    )
    assert out == option
