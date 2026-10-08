"""What every Anthropic call costs, written down as it happens.

The console bills one number per day. Deciding anything about it — which feature eats it,
whether a change made it cheaper, whether an account costs more than it pays — needs it per
call: who it was for, what it was for, which model, how many tokens. Every messages.create /
messages.stream in this codebase hands its response to record(); nothing else writes
app.db.ai_calls.

Who the call was for comes from the context, not from arguments: most AI functions take a
posting and a resume, not a user, and threading a user id through all of them would touch
every signature. A request binds its user when get_current_user resolves it (bind_request in
the app middleware + set_user in the dependency). A background thread or a worker pool does
not inherit a context, so it binds its user itself with `attributed(user_id)`. A call with no
binding is still recorded, with no user — the report shows that share, so a missing binding
is visible instead of lost.
"""

import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from decimal import Decimal

# USD per million tokens: (input, output, cache read, 5-minute cache write). Anthropic
# first-party list prices; a 1-hour cache write bills at twice the input price. Matched by
# the longest prefix of the model the response names, so dated snapshots resolve too.
# Long-context surcharges are not modelled: no prompt here comes near 100K tokens.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-sonnet-4-20250514": (3.00, 15.00, 0.30, 3.75),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30, 3.75),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}

# The request's box, or the background thread's. A dict (not the id itself) so that the
# dependency, which FastAPI runs in a worker thread on a COPY of the request's context, can
# fill in the user for the endpoint that runs in another copy: both copies hold this same
# dict.
_who: ContextVar[dict | None] = ContextVar("ai_meter_who", default=None)


def bind_request() -> Token:
    """Open an empty box for one request. Called by the app middleware."""
    return _who.set({})


def unbind(token: Token) -> None:
    _who.reset(token)


def set_user(user_id: str | None) -> None:
    """Name the user of the current request. Outside a request there is no box: no-op."""
    box = _who.get()
    if box is not None and user_id:
        box["user_id"] = str(user_id)


@contextmanager
def attributed(user_id: str | None) -> Iterator[None]:
    """Bind a user for the calls made inside — for threads and pools, which start with an
    empty context and would otherwise record their calls as nobody's."""
    token = _who.set({"user_id": str(user_id)} if user_id else {})
    try:
        yield
    finally:
        _who.reset(token)


def current_user() -> str | None:
    box = _who.get()
    return box.get("user_id") if box else None


def price_of(model: str) -> tuple[float, float, float, float] | None:
    best = max((p for p in PRICES if model.startswith(p)), key=len, default=None)
    return PRICES[best] if best else None


def _count(obj, name: str) -> int:
    # A test double's attribute is a mock, not a count — read it as no tokens.
    value = getattr(obj, name, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def cost_usd(model: str, usage) -> Decimal | None:
    """Dollars for one response's usage, or None when the model has no price here."""
    price = price_of(model)
    if price is None:
        return None
    p_in, p_out, p_read, p_write = price
    write_1h = _count(getattr(usage, "cache_creation", None), "ephemeral_1h_input_tokens")
    write_5m = max(0, _count(usage, "cache_creation_input_tokens") - write_1h)
    per_million = (
        _count(usage, "input_tokens") * p_in
        + _count(usage, "output_tokens") * p_out
        + _count(usage, "cache_read_input_tokens") * p_read
        + write_5m * p_write
        + write_1h * p_in * 2
    )
    return (Decimal(str(per_million)) / Decimal(1_000_000)).quantize(Decimal("0.000001"))


def record(message, purpose: str) -> None:
    """Write one call to the ledger. Never raises and never delays the caller: the AI path it
    measures must not fail or wait because the ledger is unreachable."""
    try:
        usage = getattr(message, "usage", None)
        model = getattr(message, "model", None)
        model = model if isinstance(model, str) else ""
        cost = cost_usd(model, usage)
        if cost is None:
            print(f"[ai-meter] no price for model {model!r} ({purpose})", file=sys.stderr)
        row = {
            "user_id": current_user(),
            "purpose": purpose,
            "model": model or "unknown",
            "input_tokens": _count(usage, "input_tokens"),
            "output_tokens": _count(usage, "output_tokens"),
            "cache_read_tokens": _count(usage, "cache_read_input_tokens"),
            "cache_write_tokens": _count(usage, "cache_creation_input_tokens"),
            "cost_usd": None if cost is None else str(cost),
        }
        _dispatch(row)
    except Exception as e:  # noqa: BLE001 — the meter must never break the call it measures
        print(f"[ai-meter] {purpose}: not recorded: {type(e).__name__}: {e}", file=sys.stderr)


def _write(row: dict) -> None:
    try:
        from app.db import ai_calls as ai_calls_db

        ai_calls_db.insert(row)
    except Exception as e:  # noqa: BLE001 — reported; a lost row must not break a thread
        print(f"[ai-meter] {row.get('purpose')}: insert failed: {e}", file=sys.stderr)


def _dispatch(row: dict) -> None:
    threading.Thread(target=_write, args=(row,), daemon=True, name="ai-meter").start()
