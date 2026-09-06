"""Billing router tests — the code path real money will flow through.

Stripe SDK is never touched: `_stripe()` is patched to return a MagicMock (or
None for the not-configured cases). billing_db is patched at the router's
reference so no Supabase chain setup is needed.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.billing_config import PLANS, plan_by_key, tier_for_price

API = "/api/v1"


# ---------------------------------------------------------------- billing_config


def test_plan_by_key_known_and_unknown():
    assert plan_by_key("weekly")["interval"] == "week"
    assert plan_by_key("MONTHLY")["interval"] == "month"  # case-insensitive
    assert plan_by_key("premium") is None
    assert plan_by_key(None) is None


def test_tier_for_price_maps_only_our_prices(monkeypatch):
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    monkeypatch.setitem(PLANS["monthly"], "price_id", "price_m")
    assert tier_for_price("price_w") == "pro"
    assert tier_for_price("price_m") == "pro"
    assert tier_for_price("price_someone_elses") is None
    assert tier_for_price("") is None


def test_tier_for_price_ignores_empty_configured_price(monkeypatch):
    # Unconfigured plan (price_id="") must never match an empty price from an event.
    monkeypatch.setitem(PLANS["weekly"], "price_id", "")
    monkeypatch.setitem(PLANS["monthly"], "price_id", "")
    assert tier_for_price("") is None


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def stripe_mock():
    """Patch _stripe() to a MagicMock and set a webhook secret."""
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


def make_event(etype: str, obj: dict, event_id: str = "evt_1"):
    return {"id": event_id, "type": etype, "data": {"object": obj}}


SUB_ACTIVE = {
    "id": "sub_1",
    "status": "active",
    "current_period_end": 1800000000,
    "items": {"data": [{"price": {"id": "price_w"}}]},
}


# ---------------------------------------------------------------- checkout


def test_checkout_503_when_stripe_not_configured(auth_client):
    with patch("app.routers.billing._stripe", return_value=None):
        r = auth_client.post(f"{API}/billing/checkout", json={"plan": "weekly"})
    assert r.status_code == 503


def test_checkout_requires_auth(client):
    r = client.post(f"{API}/billing/checkout", json={"plan": "weekly"})
    # 422 = required Authorization header missing (existing get_current_user contract)
    assert r.status_code in (401, 403, 422)


def test_checkout_400_on_unknown_plan(auth_client, stripe_mock):
    r = auth_client.post(f"{API}/billing/checkout", json={"plan": "elite"})
    assert r.status_code == 400


def test_checkout_400_when_price_unconfigured(auth_client, stripe_mock, monkeypatch):
    monkeypatch.setitem(PLANS["weekly"], "price_id", "")
    r = auth_client.post(f"{API}/billing/checkout", json={"plan": "weekly"})
    assert r.status_code == 400


def test_checkout_returns_session_url(auth_client, stripe_mock, fake_user, monkeypatch):
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    stripe_mock.checkout.Session.create.return_value.url = "https://checkout.stripe.com/c/x"

    r = auth_client.post(f"{API}/billing/checkout", json={"plan": "weekly"})

    assert r.status_code == 200
    assert r.json() == {"url": "https://checkout.stripe.com/c/x"}
    kwargs = stripe_mock.checkout.Session.create.call_args.kwargs
    assert kwargs["mode"] == "subscription"
    assert kwargs["client_reference_id"] == fake_user.id
    assert kwargs["line_items"] == [{"price": "price_w", "quantity": 1}]


def test_checkout_502_when_stripe_errors(auth_client, stripe_mock, monkeypatch):
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    stripe_mock.checkout.Session.create.side_effect = RuntimeError("stripe down")
    r = auth_client.post(f"{API}/billing/checkout", json={"plan": "weekly"})
    assert r.status_code == 502


# ---------------------------------------------------------------- webhook


def test_webhook_503_when_not_configured(client):
    with patch("app.routers.billing._stripe", return_value=None):
        r = client.post(f"{API}/billing/webhook", content=b"{}")
    assert r.status_code == 503


def test_webhook_400_on_bad_signature(client, stripe_mock, billing_db_mock):
    stripe_mock.Webhook.construct_event.side_effect = ValueError("bad sig")
    r = client.post(f"{API}/billing/webhook", content=b"{}")
    assert r.status_code == 400
    billing_db_mock.grant.assert_not_called()


def test_webhook_duplicate_event_is_skipped(client, stripe_mock, billing_db_mock):
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "checkout.session.completed", {"client_reference_id": "u1", "customer": "cus_1"}
    )
    billing_db_mock.mark_event_processed.return_value = False  # already seen
    r = client.post(f"{API}/billing/webhook", content=b"{}")
    assert r.status_code == 200
    assert r.json()["duplicate"] is True
    billing_db_mock.link_customer.assert_not_called()
    billing_db_mock.grant.assert_not_called()


def test_webhook_checkout_completed_links_and_grants(
    client, stripe_mock, billing_db_mock, monkeypatch
):
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "checkout.session.completed",
        {"client_reference_id": "u1", "customer": "cus_1", "subscription": "sub_1"},
    )
    stripe_mock.Subscription.retrieve.return_value = SUB_ACTIVE

    r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 200
    billing_db_mock.link_customer.assert_called_once_with("u1", "cus_1")
    args, kwargs = billing_db_mock.grant.call_args
    assert args[0] == "u1"
    assert args[1] == "pro"
    assert args[2].startswith("2027-")  # unix 1800000000 → ISO, tz-aware
    assert kwargs["subscription_id"] == "sub_1"


def test_webhook_unknown_price_grants_nothing(client, stripe_mock, billing_db_mock, monkeypatch):
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    monkeypatch.setitem(PLANS["monthly"], "price_id", "price_m")
    sub = {**SUB_ACTIVE, "items": {"data": [{"price": {"id": "price_foreign"}}]}}
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "checkout.session.completed",
        {"client_reference_id": "u1", "customer": "cus_1", "subscription": "sub_1"},
    )
    stripe_mock.Subscription.retrieve.return_value = sub

    r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 200
    billing_db_mock.grant.assert_not_called()


def test_webhook_subscription_updated_active_regrants(
    client, stripe_mock, billing_db_mock, monkeypatch
):
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "customer.subscription.updated", {**SUB_ACTIVE, "customer": "cus_1"}
    )
    billing_db_mock.find_user_by_customer.return_value = "u1"

    r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 200
    assert billing_db_mock.grant.call_args.args[1] == "pro"
    billing_db_mock.downgrade.assert_not_called()


def test_webhook_subscription_updated_canceled_downgrades(
    client, stripe_mock, billing_db_mock, monkeypatch
):
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "customer.subscription.updated", {**SUB_ACTIVE, "status": "canceled", "customer": "cus_1"}
    )
    billing_db_mock.find_user_by_customer.return_value = "u1"

    r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 200
    billing_db_mock.downgrade.assert_called_once_with("u1")
    billing_db_mock.grant.assert_not_called()


def test_webhook_subscription_deleted_downgrades(client, stripe_mock, billing_db_mock):
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "customer.subscription.deleted", {"id": "sub_1", "customer": "cus_1"}
    )
    billing_db_mock.find_user_by_customer.return_value = "u1"

    r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 200
    billing_db_mock.downgrade.assert_called_once_with("u1")


def test_webhook_unknown_customer_is_acked(client, stripe_mock, billing_db_mock):
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "customer.subscription.deleted", {"id": "sub_1", "customer": "cus_unknown"}
    )
    billing_db_mock.find_user_by_customer.return_value = None

    r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 200
    billing_db_mock.downgrade.assert_not_called()


def test_webhook_survives_real_stripe_sdk_objects(client, billing_db_mock, monkeypatch):
    """Regression for the live 500 on the FIRST real payment (2026-09-06): stripe-python
    v15 StripeObject is not a dict — event.get('id') raised KeyError('get') before the
    handler try. Drive the route with REAL SDK objects so the .to_dict() flattening and
    every downstream .get() is exercised against the true types."""
    import stripe as real_stripe

    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    ev = real_stripe.Event.construct_from(
        {
            "id": "evt_real_1",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": "u1",
                    "customer": "cus_1",
                    "subscription": "sub_1",
                }
            },
        },
        "sk_test_x",
    )
    sub = real_stripe.Subscription.construct_from({**SUB_ACTIVE, "customer": "cus_1"}, "sk_test_x")

    fake = MagicMock()
    fake.Webhook.construct_event.return_value = ev
    fake.Subscription.retrieve.return_value = sub
    with (
        patch("app.routers.billing._stripe", return_value=fake),
        patch("app.routers.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"),
    ):
        r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 200
    billing_db_mock.link_customer.assert_called_once_with("u1", "cus_1")
    assert billing_db_mock.grant.call_args.args[1] == "pro"


def test_webhook_handler_error_still_acks(client, stripe_mock, billing_db_mock):
    # Never 500 a webhook on our own logic error — Stripe would hammer retries.
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "checkout.session.completed",
        {"client_reference_id": "u1", "customer": "cus_1", "subscription": "sub_1"},
    )
    stripe_mock.Subscription.retrieve.side_effect = RuntimeError("boom")

    r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 200
    assert r.json()["received"] is True
