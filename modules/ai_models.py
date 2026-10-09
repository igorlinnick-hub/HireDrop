"""How to ask any of our models for a plain answer, and how to read it back.

The 5.x models think by default, and thinking spends the same max_tokens a short JSON
verdict or a cover letter needs: a reply cut off mid-answer is the visible failure. The
way to turn it off differs per model — Sonnet 5.5 rejects {"type": "disabled"} and takes
{"type": "between_tools"}, which every other model rejects; Opus 5.5 and the Fable models
cannot turn it off at all — so one table decides it for every call site, and a model
missing from it is refused rather than left thinking. A 5.x reply can also start with a
thinking block, so the answer is read by block type, never as content[0]; and a 5.x model
can decline (stop_reason "refusal"), which is no answer at all.
"""

# The thinking setting that means "answer plainly", by model (longest prefix wins). None:
# the model thinks only when asked, so nothing is sent.
_THINKING_OFF: dict[str, dict | None] = {
    "claude-haiku-4-5": None,
    "claude-sonnet-4": None,
    "claude-haiku-5-5": {"type": "disabled"},
    "claude-sonnet-5": {"type": "disabled"},
    "claude-sonnet-5-5": {"type": "between_tools"},
}


def plain_answer_kwargs(model: str) -> dict:
    """Extra Messages fields that make `model` answer without extended thinking. Raises
    ValueError for a model the table does not know how to stop thinking."""
    best = max((p for p in _THINKING_OFF if model.startswith(p)), key=len, default=None)
    if best is None:
        raise ValueError(f"no known way to ask {model} for a plain answer (modules/ai_models.py)")
    off = _THINKING_OFF[best]
    return {"thinking": off} if off else {}


def reply_text(message) -> str:
    """The text of a Messages reply, skipping thinking blocks (content[0] may be one)."""
    return "".join(b.text for b in message.content if getattr(b, "type", "text") == "text").strip()


def refused(message) -> bool:
    return getattr(message, "stop_reason", None) == "refusal"
