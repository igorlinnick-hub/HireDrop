"""How to ask any of our models for a plain answer, and how to read it back.

The 5.x models think by default, and thinking spends the same max_tokens a short JSON
verdict or a cover letter needs: a reply cut off mid-answer is the visible failure. The
way to turn it off differs per model — Sonnet 5.5 rejects {"type": "disabled"} and takes
{"type": "between_tools"}, which every other model rejects — so one function decides it
for every call site. A 5.x reply can also start with a thinking block, so the answer is
read by block type, never as content[0]; and a 5.x model can decline (stop_reason
"refusal"), which is no answer at all.
"""

# Sonnet 5.5 only accepts this as "no extended thinking".
NO_THINKING = {"type": "between_tools"}


def plain_answer_kwargs(model: str) -> dict:
    """Extra Messages fields that make `model` answer without extended thinking."""
    if model.startswith("claude-sonnet-5-5"):
        return {"thinking": NO_THINKING}
    if model.startswith(("claude-haiku-5-5", "claude-sonnet-5")):
        return {"thinking": {"type": "disabled"}}
    return {}


def reply_text(message) -> str:
    """The text of a Messages reply, skipping thinking blocks (content[0] may be one)."""
    return "".join(b.text for b in message.content if getattr(b, "type", "text") == "text").strip()


def refused(message) -> bool:
    return getattr(message, "stop_reason", None) == "refusal"
