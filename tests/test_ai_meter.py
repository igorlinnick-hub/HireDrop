"""The AI call ledger: what one response costs, who it is charged to, and that metering can
never break or delay the call it measures (modules/ai_meter.py)."""

import threading
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from modules import ai_meter


def _message(model="claude-haiku-4-5-20251001", **usage):
    fields = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    fields.update(usage)
    return SimpleNamespace(model=model, usage=SimpleNamespace(**fields))


def test_a_haiku_call_costs_its_list_price():
    # 2,000 in x $1/M + 300 out x $5/M = $0.002 + $0.0015
    msg = _message(input_tokens=2000, output_tokens=300)
    assert ai_meter.cost_usd(msg.model, msg.usage) == Decimal("0.003500")


def test_cache_reads_and_writes_are_priced_apart_from_plain_input():
    # Sonnet 4.6: 1,000 read x $0.30/M + 2,000 written (5 min) x $3.75/M
    msg = _message(
        "claude-sonnet-4-6", cache_read_input_tokens=1000, cache_creation_input_tokens=2000
    )
    assert ai_meter.cost_usd(msg.model, msg.usage) == Decimal("0.007800")


def test_a_one_hour_cache_write_bills_double_the_input_price():
    usage = SimpleNamespace(
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=1000,
        cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=1000),
    )
    assert ai_meter.cost_usd("claude-haiku-4-5", usage) == Decimal("0.002000")


def test_the_longest_matching_price_wins():
    assert ai_meter.price_of("claude-sonnet-5-5") == ai_meter.PRICES["claude-sonnet-5-5"]
    assert ai_meter.price_of("claude-sonnet-5") == ai_meter.PRICES["claude-sonnet-5"]
    assert ai_meter.price_of("claude-sonnet-4-20250514") == (3.00, 15.00, 0.30, 3.75)


def test_a_model_without_a_price_is_recorded_with_no_cost_not_zero(ai_meter_rows):
    ai_meter.record(_message("claude-unknown-9", input_tokens=10), "fit_judge")
    assert ai_meter_rows[0]["cost_usd"] is None
    assert ai_meter_rows[0]["model"] == "claude-unknown-9"


def test_a_call_inside_attributed_is_charged_to_that_user(ai_meter_rows):
    with ai_meter.attributed("user-a"):
        ai_meter.record(_message(input_tokens=1), "job_score")
    ai_meter.record(_message(input_tokens=1), "job_score")
    assert [r["user_id"] for r in ai_meter_rows] == ["user-a", None]
    assert ai_meter_rows[0]["purpose"] == "job_score"


def test_a_worker_thread_does_not_inherit_the_binding(ai_meter_rows):
    # Why every pool and thread binds its own user: the context does not travel.
    with ai_meter.attributed("user-a"):
        t = threading.Thread(target=ai_meter.record, args=(_message(), "fit_judge"))
        t.start()
        t.join()
    assert ai_meter_rows[0]["user_id"] is None


def test_a_request_charges_its_calls_to_the_user_the_auth_dependency_resolved(ai_meter_rows):
    # The endpoint and the dependency run on separate copies of the request's context —
    # the box opened by the middleware is what carries the user across.
    from app.main import AIMeterScope

    app = FastAPI()
    app.add_middleware(AIMeterScope)

    def who():
        ai_meter.set_user("user-b")
        return "user-b"

    @app.get("/x")
    def endpoint(_user=Depends(who)):
        ai_meter.record(_message(input_tokens=5), "cover_letter")
        return {}

    TestClient(app).get("/x")
    assert ai_meter_rows[0]["user_id"] == "user-b"


def test_get_current_user_names_the_user_for_the_meter():
    from app import deps

    token = ai_meter.bind_request()
    try:
        with patch.object(
            deps.ext_keys_db, "verify", return_value={"user_id": "user-c", "email": "c@x.io"}
        ):
            deps.get_current_user("Bearer hd_abc")
        assert ai_meter.current_user() == "user-c"
    finally:
        ai_meter.unbind(token)


def test_set_user_outside_a_request_is_a_no_op():
    ai_meter.set_user("user-d")
    assert ai_meter.current_user() is None


def test_a_broken_response_is_reported_never_raised(ai_meter_rows, capsys):
    ai_meter.record(None, "buddy")
    # No usage at all reads as zero tokens of an unknown model — recorded, not lost.
    assert ai_meter_rows[0]["input_tokens"] == 0
    assert ai_meter_rows[0]["cost_usd"] is None
    assert "no price" in capsys.readouterr().err


def test_a_test_double_response_records_without_crashing(ai_meter_rows):
    ai_meter.record(MagicMock(), "screener_answer")
    assert ai_meter_rows[0]["input_tokens"] == 0


def test_an_unreachable_ledger_is_reported_and_swallowed(capsys):
    with patch("app.db.ai_calls.insert", side_effect=RuntimeError("supabase down")):
        ai_meter._write({"purpose": "fit_judge"})
    assert "insert failed" in capsys.readouterr().err


def test_every_anthropic_call_site_records_itself():
    # A contract over the code, not a behavior: a new messages.create that skips the meter
    # is spend the ledger never sees, and the report would under-count it silently.
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    missing = []
    for path in [*root.glob("modules/**/*.py"), *root.glob("app/**/*.py")]:
        text = path.read_text()
        calls = len(re.findall(r"\.messages\.(create|stream)\(", text))
        records = len(re.findall(r"ai_meter\.record\(", text))
        if calls > records:
            missing.append(f"{path.relative_to(root)}: {calls} calls, {records} records")
    assert not missing, missing
