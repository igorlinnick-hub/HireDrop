"""No long dashes in text an employer reads (Igor's rule, 2026-10-08).

The dash cases live in chrome-extension/tests/fixtures/no-long-dashes-cases.json, shared
with the extension's belt copy (no-long-dashes.test.js) so the two cannot drift apart.
"""

import json
from pathlib import Path

import pytest

from modules.ai_cover_letter import to_plain_letter
from modules.text_style import has_long_dash, no_long_dashes

_CASES = json.loads(
    (
        Path(__file__).parents[1]
        / "chrome-extension"
        / "tests"
        / "fixtures"
        / "no-long-dashes-cases.json"
    ).read_text()
)


@pytest.mark.parametrize(("raw", "clean"), _CASES)
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
