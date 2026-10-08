"""Text an employer reads carries no long dashes.

Igor's rule (2026-10-08, "это правило"): no em dash, no en dash, no "--" in anything we
write under the person's name. A long dash is the most recognisable tell of model-written
text, and an employer who spots it reads the whole application as a bot's.

The letter prompt has said "NO em-dashes" since August and letters still went out with
them; the screener answerer never said it at all. A prompt line is a request. This is the
guarantee: every generated text an employer reads passes through no_long_dashes() on the
way out (modules/ai_cover_letter.to_plain_letter, modules/ai_question_answer, the
/tools/answer-question cache leg). A new generation path must call it too.

chrome-extension/background.js carries a hand-ported noLongDashes() as the belt on the
last hop. The two must agree: both run the shared table in
chrome-extension/tests/fixtures/no-long-dashes-cases.json (tests/test_text_style.py and
chrome-extension/tests/no-long-dashes.test.js) — extend the table, not one copy.
"""

import re

# A range keeps a plain hyphen, not a comma: digits ("2019–2021", "10 — 20", "2019--2021")
# and an UNSPACED en dash between word characters ("Mon–Fri", "June–August"). An unspaced
# em dash stays a comma — that one is parenthetical ("Growth work—mostly"), not a range.
_RANGE = re.compile(r"(\d)[ \t]*(?:[–—]|--)[ \t]*(\d)")
_WORD_RANGE = re.compile(r"(\w)–(\w)")
# "--" but not part of a "---" rule (to_plain_letter drops those lines).
_LONG = r"(?:—|–|(?<!-)--(?!-))"
_LEADING = re.compile(rf"(?m)^([ \t]*){_LONG}[ \t]*")
_EOL = re.compile(rf"(?m)[ \t]*{_LONG}[ \t]*$")
_INLINE = re.compile(rf"[ \t]*{_LONG}[ \t]*")
_PUNCT = ".,;:!?"


def _comma(m: re.Match) -> str:
    """The replacement for one inline dash, read in context — never touching commas the
    text already had ("e.g.," stays "e.g.,")."""
    s = m.string
    nxt = s[m.end()] if m.end() < len(s) else ""
    if nxt in _PUNCT:  # "scaled it--." → "scaled it."
        return ""
    prev = s[m.start() - 1] if m.start() else ""
    if prev in _PUNCT:  # "goal. — Next" → "goal. Next"
        return " "
    return ", "


def no_long_dashes(text: str) -> str:
    """Replace every em dash, en dash and "--" with what a person would type instead:
    a hyphen inside a range, nothing at a line edge, a comma elsewhere."""
    if not text:
        return text
    text = _RANGE.sub(r"\1-\2", text)
    text = _WORD_RANGE.sub(r"\1-\2", text)
    text = _LEADING.sub(r"\1", text)
    text = _EOL.sub("", text)
    return _INLINE.sub(_comma, text)


def has_long_dash(text: str) -> bool:
    return bool(text) and bool(re.search(_LONG, text))
