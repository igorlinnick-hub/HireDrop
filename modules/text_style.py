"""Text an employer reads carries no long dashes.

Igor's rule (2026-10-08, "это правило"): no em dash, no en dash, no "--" in anything we
write under the person's name. A long dash is the most recognisable tell of model-written
text, and an employer who spots it reads the whole application as a bot's.

The letter prompt has said "NO em-dashes" since August and letters still went out with
them; the screener answerer never said it at all. A prompt line is a request. This is the
guarantee: every generated text an employer reads passes through no_long_dashes() on the
way out (modules/ai_cover_letter.to_plain_letter, modules/ai_question_answer, the
/tools/answer-question cache leg). A new generation path must call it too.
"""

import re

# "2019–2021", "10 — 20": a range keeps a plain hyphen, not a comma.
_RANGE = re.compile(r"(\d)[ \t]*[–—][ \t]*(\d)")
# "--" but not part of a "---" rule (to_plain_letter drops those lines).
_LONG = r"(?:—|–|(?<!-)--(?!-))"
_LEADING = re.compile(rf"(?m)^([ \t]*){_LONG}[ \t]*")
_EOL = re.compile(rf"(?m)[ \t]*{_LONG}[ \t]*$")
_INLINE = re.compile(rf"[ \t]*{_LONG}[ \t]*")
# The comma we put in, next to punctuation already there: "done. , next" / "so , ,".
_STACKED = re.compile(r"([.,;:!?])[ \t]*,")


def no_long_dashes(text: str) -> str:
    """Replace every em dash, en dash and "--" with what a person would type instead:
    a hyphen inside a number range, nothing at the start of a line, a comma elsewhere."""
    if not text:
        return text
    text = _RANGE.sub(r"\1-\2", text)
    text = _LEADING.sub(r"\1", text)
    text = _EOL.sub("", text)
    text = _INLINE.sub(", ", text)
    return _STACKED.sub(r"\1", text)


def has_long_dash(text: str) -> bool:
    return bool(text) and bool(re.search(_LONG, text))
