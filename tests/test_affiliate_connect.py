"""Stripe Connect payouts — the IDOR surface and the money-safety primitives
Igor asked to see proven before this merges (2026-10-01):

  - an affiliate can only ever touch THEIR OWN Connect account (no id in the
    request body, every write filtered by the verified caller's user_id)
  - `account.updated` can only flip payouts_enabled for the account the EVENT
    itself names, never one a client supplies
  - the daily payout job claims commissions before calling Stripe, and reuses
    the same idempotency key on retry, so a crash mid-run cannot pay twice
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.db import affiliates as affiliates_db

API = "/api/v1"


class FakeTable:
    """Chainable PostgREST stand-in that also records which `.eq()` filters
    were in effect when a write happened — the thing that proves a write is
    scoped to the right row, not just that *a* write happened."""

    def __init__(self, name: str, rows: list, log: list):
        self.name, self._rows, self.log = name, rows, log
        self._filters: list[tuple] = []
        self._pending: tuple | None = None

    def select(self, *a, **k):
        return self

    def eq(self, field, value):
        self._filters.append((field, value))
        return self

    def limit(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def update(self, patch, **k):
        self._pending = ("update", patch)
        return self

    def insert(self, row, **k):
        self._pending = ("insert", row)
        return self

    def execute(self):
        # Filters apply via .eq() AFTER .update()/.insert() in the real client's
        # fluent chain, so the write is only logged (with whatever filters
        # accumulated by then) once .execute() actually runs.
        if self._pending:
            op, payload = self._pending
            self.log.append((op, self.name, payload, list(self._filters)))
        return SimpleNamespace(data=self._rows)


@pytest.fixture
def fake_db():
    tables: dict[str, list] = {}
    log: list = []
    client = MagicMock()
    client.table.side_effect = lambda name: FakeTable(name, tables.get(name, []), log)
    with patch("app.db.affiliates.get_supabase", return_value=client):
        yield SimpleNamespace(tables=tables, log=log)


# --------------------------------------------------------------- db helpers


def test_save_stripe_account_is_filtered_by_user_id_not_affiliate_id(fake_db):
    affiliates_db.save_stripe_account("user_1", "acct_123")

    (op, table, patch_, filters) = fake_db.log[0]
    assert (op, table) == ("update", "affiliates")
    assert patch_ == {"stripe_account_id": "acct_123"}
    assert filters == [("user_id", "user_1")]


def test_set_payouts_enabled_is_filtered_by_the_events_own_account_id(fake_db):
    affiliates_db.set_payouts_enabled("acct_from_event", True)

    (op, table, patch_, filters) = fake_db.log[0]
    assert (op, table) == ("update", "affiliates")
    assert patch_ == {"payouts_enabled": True}
    assert filters == [("stripe_account_id", "acct_from_event")]


def test_affiliate_for_user_reads_the_callers_own_row(fake_db):
    fake_db.tables["affiliates"] = [{"id": "aff_1", "user_id": "user_1", "code": "igor"}]
    row = affiliates_db.affiliate_for_user("user_1")
    assert row["code"] == "igor"


# -------------------------------------------------------------- the endpoint


@pytest.fixture
def stripe_mock():
    fake = MagicMock()
    with patch("app.routers.affiliate._stripe", return_value=fake):
        yield fake


@pytest.fixture
def affiliates_db_mock():
    fake = MagicMock()
    with patch("app.routers.affiliate.affiliates_db", fake):
        yield fake


def test_connect_404s_for_someone_who_is_not_an_affiliate(
    auth_client, stripe_mock, affiliates_db_mock
):
    affiliates_db_mock.affiliate_for_user.return_value = None
    r = auth_client.post(f"{API}/affiliate/payouts/connect")
    assert r.status_code == 404


def test_connect_creates_an_account_when_none_exists_yet(
    auth_client, stripe_mock, affiliates_db_mock, fake_user
):
    affiliates_db_mock.affiliate_for_user.return_value = {"stripe_account_id": None}
    stripe_mock.Account.create.return_value = SimpleNamespace(id="acct_new")
    stripe_mock.AccountLink.create.return_value = SimpleNamespace(
        url="https://connect.stripe.com/x"
    )

    r = auth_client.post(f"{API}/affiliate/payouts/connect")

    assert r.status_code == 200
    assert r.json() == {"url": "https://connect.stripe.com/x"}
    stripe_mock.Account.create.assert_called_once()
    # The IDOR guarantee: the fresh account is attached to the CALLER's row —
    # there is no affiliate/account id anywhere in this request to substitute.
    affiliates_db_mock.save_stripe_account.assert_called_once_with(fake_user.id, "acct_new")
    assert stripe_mock.AccountLink.create.call_args.kwargs["account"] == "acct_new"


def test_connect_reuses_an_existing_account_instead_of_making_a_second_one(
    auth_client, stripe_mock, affiliates_db_mock
):
    affiliates_db_mock.affiliate_for_user.return_value = {"stripe_account_id": "acct_existing"}
    stripe_mock.AccountLink.create.return_value = SimpleNamespace(
        url="https://connect.stripe.com/y"
    )

    r = auth_client.post(f"{API}/affiliate/payouts/connect")

    assert r.status_code == 200
    stripe_mock.Account.create.assert_not_called()
    affiliates_db_mock.save_stripe_account.assert_not_called()
    assert stripe_mock.AccountLink.create.call_args.kwargs["account"] == "acct_existing"


def test_connect_looks_up_only_the_callers_own_affiliate_row(
    auth_client, stripe_mock, affiliates_db_mock, fake_user
):
    """Nothing in this request names an affiliate — the only identity in play
    is the verified JWT's user id, so there is no field to tamper with to
    reach someone else's Connect account."""
    affiliates_db_mock.affiliate_for_user.return_value = {"stripe_account_id": "acct_existing"}
    stripe_mock.AccountLink.create.return_value = SimpleNamespace(url="https://x")

    auth_client.post(f"{API}/affiliate/payouts/connect")

    affiliates_db_mock.affiliate_for_user.assert_called_once_with(fake_user.id)


def test_connect_502s_when_account_creation_fails(auth_client, stripe_mock, affiliates_db_mock):
    affiliates_db_mock.affiliate_for_user.return_value = {"stripe_account_id": None}
    stripe_mock.Account.create.side_effect = Exception("stripe is down")

    r = auth_client.post(f"{API}/affiliate/payouts/connect")

    assert r.status_code == 502
    affiliates_db_mock.save_stripe_account.assert_not_called()


def test_connect_503s_when_stripe_is_not_configured(auth_client, affiliates_db_mock):
    with patch("app.routers.affiliate._stripe", return_value=None):
        r = auth_client.post(f"{API}/affiliate/payouts/connect")
    assert r.status_code == 503


def test_unauthenticated_caller_cannot_reach_the_endpoint(client, stripe_mock, affiliates_db_mock):
    r = client.post(f"{API}/affiliate/payouts/connect")
    # 422 (FastAPI rejecting the missing required Authorization header) is this
    # codebase's normal shape for "no credential at all" — see app/deps.py.
    assert r.status_code in (401, 403, 422)


# ----------------------------------------------------------- webhook wiring


@pytest.fixture
def billing_stripe_mock():
    fake = MagicMock()
    with (
        patch("app.routers.billing._stripe", return_value=fake),
        patch("app.routers.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"),
    ):
        yield fake


@pytest.fixture
def billing_db_mock():
    fake = MagicMock()
    fake.claim_event.return_value = True
    with patch("app.routers.billing.billing_db", fake):
        yield fake


def test_account_updated_flips_payouts_enabled_from_the_events_own_account_id(
    client, billing_stripe_mock, billing_db_mock
):
    billing_stripe_mock.Webhook.construct_event.return_value = {
        "id": "evt_acct",
        "type": "account.updated",
        "data": {"object": {"id": "acct_123", "payouts_enabled": True}},
    }

    with patch("app.routers.billing.affiliates_db") as aff:
        r = client.post(f"{API}/billing/webhook", content=b"{}", headers={"stripe-signature": "x"})

    assert r.status_code == 200
    aff.set_payouts_enabled.assert_called_once_with("acct_123", True)


def test_account_updated_with_payouts_still_disabled(client, billing_stripe_mock, billing_db_mock):
    billing_stripe_mock.Webhook.construct_event.return_value = {
        "id": "evt_acct2",
        "type": "account.updated",
        "data": {"object": {"id": "acct_123", "payouts_enabled": False}},
    }

    with patch("app.routers.billing.affiliates_db") as aff:
        client.post(f"{API}/billing/webhook", content=b"{}", headers={"stripe-signature": "x"})

    aff.set_payouts_enabled.assert_called_once_with("acct_123", False)


def test_redelivered_account_updated_event_is_deduped(client, billing_stripe_mock, billing_db_mock):
    """Same protection every other event type gets: claim_event() says this id
    was already processed, so a Stripe retry never re-runs the handler."""
    billing_db_mock.claim_event.return_value = False
    billing_stripe_mock.Webhook.construct_event.return_value = {
        "id": "evt_acct",
        "type": "account.updated",
        "data": {"object": {"id": "acct_123", "payouts_enabled": True}},
    }

    with patch("app.routers.billing.affiliates_db") as aff:
        r = client.post(f"{API}/billing/webhook", content=b"{}", headers={"stripe-signature": "x"})

    assert r.json() == {"received": True, "duplicate": True}
    aff.set_payouts_enabled.assert_not_called()


# ------------------------------------------- two endpoints, two signing secrets


def _signed(payload: dict, secret: str) -> tuple[bytes, str]:
    """Sign a payload exactly the way Stripe does (t=..,v1=HMAC-SHA256)."""
    import hashlib
    import hmac
    import json
    import time

    body = json.dumps(payload).encode()
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={ts},v1={sig}"


@pytest.fixture
def real_stripe_two_secrets():
    import stripe

    with (
        patch("app.routers.billing._stripe", return_value=stripe),
        patch("app.routers.billing.STRIPE_WEBHOOK_SECRET", "whsec_account"),
        patch("app.routers.billing.STRIPE_CONNECT_WEBHOOK_SECRET", "whsec_connect"),
    ):
        yield


ACCOUNT_UPDATED = {
    "id": "evt_connect_1",
    "object": "event",
    "type": "account.updated",
    "account": "acct_partner",
    "data": {"object": {"id": "acct_partner", "payouts_enabled": True}},
}


def test_account_updated_signed_by_the_connect_endpoint_is_accepted(
    client, real_stripe_two_secrets, billing_db_mock
):
    body, sig = _signed(ACCOUNT_UPDATED, "whsec_connect")
    with patch("app.routers.billing.affiliates_db") as aff:
        r = client.post(f"{API}/billing/webhook", content=body, headers={"stripe-signature": sig})

    assert r.status_code == 200
    aff.set_payouts_enabled.assert_called_once_with("acct_partner", True)


def test_payment_events_still_verify_with_the_account_secret(
    client, real_stripe_two_secrets, billing_db_mock
):
    event = {
        "id": "evt_refund",
        "object": "event",
        "type": "charge.refunded",
        "data": {"object": {"invoice": "in_1"}},
    }
    body, sig = _signed(event, "whsec_account")
    with patch("app.routers.billing.affiliates_db") as aff:
        r = client.post(f"{API}/billing/webhook", content=body, headers={"stripe-signature": sig})

    assert r.status_code == 200
    aff.reverse_for_invoice.assert_called_once_with("in_1")


def test_connect_secret_cannot_carry_a_payment_event(
    client, real_stripe_two_secrets, billing_db_mock
):
    """The connect endpoint only subscribes to account.updated — anything else
    signed with its secret is dropped before it can grant a tier or a commission."""
    event = {
        "id": "evt_sneaky",
        "object": "event",
        "type": "invoice.paid",
        "data": {"object": {"customer": "cus_1", "id": "in_1"}},
    }
    body, sig = _signed(event, "whsec_connect")
    with patch("app.routers.billing.affiliates_db") as aff:
        r = client.post(f"{API}/billing/webhook", content=body, headers={"stripe-signature": sig})

    assert r.json() == {"received": True, "ignored": True}
    billing_db_mock.claim_event.assert_not_called()
    aff.accrue_from_invoice.assert_not_called()


def test_signature_from_neither_secret_is_rejected(
    client, real_stripe_two_secrets, billing_db_mock
):
    body, sig = _signed(ACCOUNT_UPDATED, "whsec_somebody_else")
    with patch("app.routers.billing.affiliates_db") as aff:
        r = client.post(f"{API}/billing/webhook", content=body, headers={"stripe-signature": sig})

    assert r.status_code == 400
    aff.set_payouts_enabled.assert_not_called()


def test_without_a_connect_secret_the_account_endpoint_works_alone(client, billing_db_mock):
    import stripe

    event = {**ACCOUNT_UPDATED, "id": "evt_platform"}
    body, sig = _signed(event, "whsec_account")
    with (
        patch("app.routers.billing._stripe", return_value=stripe),
        patch("app.routers.billing.STRIPE_WEBHOOK_SECRET", "whsec_account"),
        patch("app.routers.billing.STRIPE_CONNECT_WEBHOOK_SECRET", ""),
        patch("app.routers.billing.affiliates_db") as aff,
    ):
        r = client.post(f"{API}/billing/webhook", content=body, headers={"stripe-signature": sig})

    assert r.status_code == 200
    aff.set_payouts_enabled.assert_called_once_with("acct_partner", True)
