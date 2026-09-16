"""Affiliate commission tests — the path that decides what a partner is owed.

The ledger is money, so the rules that matter are the negative ones: no referral,
an inactive affiliate or a $0 invoice must write NOTHING. Supabase is faked with
a chainable stub (every query builder method returns self) that records writes.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.db import affiliates as affiliates_db

API = "/api/v1"


class FakeTable:
    """Chainable PostgREST stand-in: reads return `rows`, writes go to `log`."""

    def __init__(self, name: str, rows: list, log: list):
        self.name, self._rows, self.log = name, rows, log

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def upsert(self, row, **k):
        self.log.append(("upsert", self.name, row))
        return self

    def insert(self, row, **k):
        self.log.append(("insert", self.name, row))
        return self

    def update(self, patch, **k):
        self.log.append(("update", self.name, patch))
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)


@pytest.fixture
def stripe_mock():
    """Same shape as test_billing's: patch _stripe() and the signing secret."""
    fake = MagicMock()
    with (
        patch("app.routers.billing._stripe", return_value=fake),
        patch("app.routers.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"),
    ):
        yield fake


@pytest.fixture
def billing_db_mock():
    fake = MagicMock()
    fake.mark_event_processed.return_value = True  # default: first delivery
    with patch("app.routers.billing.billing_db", fake):
        yield fake


@pytest.fixture
def fake_db():
    """Patch get_supabase(); `tables` maps table name -> rows its reads return."""
    tables: dict[str, list] = {}
    log: list = []

    client = MagicMock()
    client.table.side_effect = lambda name: FakeTable(name, tables.get(name, []), log)

    with patch("app.db.affiliates.get_supabase", return_value=client):
        yield SimpleNamespace(tables=tables, log=log)


REFERRAL = [{"id": "ref_1", "affiliate_id": "aff_1", "first_paid_at": None}]
AFFILIATE = [{"id": "aff_1", "user_id": "partner", "commission_pct": 30, "status": "active"}]
INVOICE = {
    "id": "in_1",
    "amount_paid": 3900,
    "currency": "USD",
    "lines": {"data": [{"period": {"start": 1800000000, "end": 1802592000}}]},
}


def commissions_written(log):
    return [row for op, table, row in log if table == "commissions" and op == "upsert"]


# ------------------------------------------------------------------- accrual


def test_accrual_writes_the_percentage_of_what_was_collected(fake_db):
    fake_db.tables["referrals"] = REFERRAL
    fake_db.tables["affiliates"] = AFFILIATE

    affiliates_db.accrue_from_invoice("buyer", INVOICE)

    (row,) = commissions_written(fake_db.log)
    assert row["gross_cents"] == 3900
    assert row["amount_cents"] == 1170  # 30% of $39.00
    assert row["commission_pct"] == 30
    assert row["stripe_invoice_id"] == "in_1"
    assert row["currency"] == "usd"
    assert row["period_start"].startswith("2027-01-15")


def test_accrual_rounds_to_whole_cents(fake_db):
    fake_db.tables["referrals"] = REFERRAL
    fake_db.tables["affiliates"] = [{**AFFILIATE[0], "commission_pct": 30}]

    affiliates_db.accrue_from_invoice("buyer", {**INVOICE, "amount_paid": 1200})

    (row,) = commissions_written(fake_db.log)
    assert row["amount_cents"] == 360  # 30% of $12.00, no fractional cents


def test_accrual_marks_the_referral_as_paying(fake_db):
    fake_db.tables["referrals"] = REFERRAL
    fake_db.tables["affiliates"] = AFFILIATE

    affiliates_db.accrue_from_invoice("buyer", INVOICE)

    updates = [row for op, table, row in fake_db.log if table == "referrals" and op == "update"]
    assert updates == [
        {"status": "paying", "first_paid_at": updates[0]["first_paid_at"]},
    ]
    assert updates[0]["first_paid_at"]  # stamped on the first payment


def test_accrual_keeps_the_original_first_paid_at(fake_db):
    fake_db.tables["referrals"] = [{**REFERRAL[0], "first_paid_at": "2026-01-01T00:00:00+00:00"}]
    fake_db.tables["affiliates"] = AFFILIATE

    affiliates_db.accrue_from_invoice("buyer", INVOICE)

    updates = [row for op, table, row in fake_db.log if table == "referrals" and op == "update"]
    assert updates == [{"status": "paying"}]  # renewal must not move the first-paid date


def test_no_referral_means_no_commission(fake_db):
    fake_db.tables["referrals"] = []

    affiliates_db.accrue_from_invoice("buyer", INVOICE)

    assert commissions_written(fake_db.log) == []


def test_inactive_affiliate_earns_nothing(fake_db):
    fake_db.tables["referrals"] = REFERRAL
    fake_db.tables["affiliates"] = [{**AFFILIATE[0], "status": "banned"}]

    affiliates_db.accrue_from_invoice("buyer", INVOICE)

    assert commissions_written(fake_db.log) == []


def test_zero_dollar_invoice_earns_nothing(fake_db):
    fake_db.tables["referrals"] = REFERRAL
    fake_db.tables["affiliates"] = AFFILIATE

    affiliates_db.accrue_from_invoice("buyer", {**INVOICE, "amount_paid": 0})

    assert commissions_written(fake_db.log) == []


def test_self_referral_earns_nothing(fake_db):
    fake_db.tables["referrals"] = REFERRAL
    fake_db.tables["affiliates"] = AFFILIATE

    affiliates_db.accrue_from_invoice("partner", INVOICE)  # the affiliate's own user id

    assert commissions_written(fake_db.log) == []


# ----------------------------------------------------------------- clawback


def test_refund_reverses_an_accrued_commission(fake_db):
    fake_db.tables["commissions"] = [
        {
            "id": "c_1",
            "status": "accrued",
            "affiliate_id": "aff_1",
            "referral_id": "ref_1",
            "amount_cents": 1170,
        }
    ]

    affiliates_db.reverse_for_invoice("in_1")

    updates = [(table, row) for op, table, row in fake_db.log if op == "update"]
    assert ("commissions", {"status": "reversed"}) in updates
    assert ("referrals", {"status": "refunded"}) in updates


def test_refund_does_not_touch_money_already_paid_out(fake_db):
    fake_db.tables["commissions"] = [
        {
            "id": "c_1",
            "status": "paid_out",
            "affiliate_id": "aff_1",
            "referral_id": "ref_1",
            "amount_cents": 1170,
        }
    ]

    affiliates_db.reverse_for_invoice("in_1")

    # That money has left our PayPal — reclaiming it is a conversation, not a write.
    assert [op for op, _, _ in fake_db.log if op == "update"] == []


def test_reversal_without_an_invoice_id_is_a_noop(fake_db):
    affiliates_db.reverse_for_invoice(None)
    assert fake_db.log == []


# ------------------------------------------------------- webhook wiring


def test_invoice_paid_accrues(client, stripe_mock, billing_db_mock):
    """The webhook must hang accrual off invoice.paid — money, not signup."""
    billing_db_mock.find_user_by_customer.return_value = "buyer"
    stripe_mock.Webhook.construct_event.return_value = {
        "id": "evt_a",
        "type": "invoice.paid",
        "data": {"object": {"customer": "cus_1", **INVOICE}},
    }
    stripe_mock.Subscription.list.return_value = {"data": []}

    with patch("app.routers.billing.affiliates_db") as aff:
        r = client.post(f"{API}/billing/webhook", content=b"{}", headers={"stripe-signature": "x"})

    assert r.status_code == 200
    assert aff.accrue_from_invoice.call_args[0][0] == "buyer"


def test_charge_refunded_reverses(client, stripe_mock, billing_db_mock):
    stripe_mock.Webhook.construct_event.return_value = {
        "id": "evt_b",
        "type": "charge.refunded",
        "data": {"object": {"invoice": "in_1"}},
    }

    with patch("app.routers.billing.affiliates_db") as aff:
        r = client.post(f"{API}/billing/webhook", content=b"{}", headers={"stripe-signature": "x"})

    assert r.status_code == 200
    aff.reverse_for_invoice.assert_called_once_with("in_1")
