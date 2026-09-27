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
    fake.claim_event.return_value = True  # default: first delivery, claim is ours
    with patch("app.routers.billing.billing_db", fake):
        yield fake


@pytest.fixture(autouse=True)
def clean_billing_row():
    """Default for every test here: the profile carries no Stripe ids (first-timer).
    Tests about a returning subscriber patch _billing_row again, which wins.
    """
    with patch("app.routers.billing._billing_row", return_value={}):
        yield


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


def test_checkout_reuses_an_existing_customer(auth_client, stripe_mock, monkeypatch):
    # One customer per user is what keeps every invoice and subscription inside ONE
    # portal session. A fresh customer each time is how a first subscription became
    # invisible — and uncancellable — for the user.
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    stripe_mock.checkout.Session.create.return_value.url = "https://checkout.stripe.com/c/x"
    with patch("app.routers.billing._billing_row", return_value={"stripe_customer_id": "cus_1"}):
        r = auth_client.post(f"{API}/billing/checkout", json={"plan": "weekly"})

    assert r.status_code == 200
    kwargs = stripe_mock.checkout.Session.create.call_args.kwargs
    assert kwargs["customer"] == "cus_1"
    assert "customer_email" not in kwargs  # Stripe rejects both together


def test_checkout_sends_email_only_when_there_is_no_customer_yet(
    auth_client, stripe_mock, fake_user, monkeypatch
):
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    stripe_mock.checkout.Session.create.return_value.url = "https://checkout.stripe.com/c/x"
    r = auth_client.post(f"{API}/billing/checkout", json={"plan": "weekly"})
    assert r.status_code == 200
    kwargs = stripe_mock.checkout.Session.create.call_args.kwargs
    assert kwargs["customer_email"] == fake_user.email
    assert "customer" not in kwargs


def test_checkout_409_when_already_subscribed(auth_client, stripe_mock, monkeypatch):
    # The dashboard hides the plan buttons from subscribers, but only if /stats answered.
    # When that read fails the buttons show — and a second checkout means two charges a
    # month with the first subscription unreachable from the portal.
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    stripe_mock.Subscription.retrieve.return_value = {"id": "sub_1", "status": "active"}
    with patch(
        "app.routers.billing._billing_row",
        return_value={"stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1"},
    ):
        r = auth_client.post(f"{API}/billing/checkout", json={"plan": "weekly"})

    assert r.status_code == 409
    assert r.json()["error"] == "already_subscribed"
    stripe_mock.checkout.Session.create.assert_not_called()


def test_checkout_409_covers_a_card_stripe_is_still_retrying(auth_client, stripe_mock, monkeypatch):
    # past_due is still a billing relationship: Stripe keeps retrying the card.
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    stripe_mock.Subscription.retrieve.return_value = {"id": "sub_1", "status": "past_due"}
    with patch(
        "app.routers.billing._billing_row",
        return_value={"stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1"},
    ):
        r = auth_client.post(f"{API}/billing/checkout", json={"plan": "weekly"})
    assert r.status_code == 409


def test_checkout_proceeds_after_a_cancelled_subscription(auth_client, stripe_mock, monkeypatch):
    # A churned user must be able to come back — the guard is against DOUBLE billing,
    # not against subscribing again.
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    stripe_mock.Subscription.retrieve.return_value = {"id": "sub_1", "status": "canceled"}
    stripe_mock.checkout.Session.create.return_value.url = "https://checkout.stripe.com/c/x"
    with patch(
        "app.routers.billing._billing_row",
        return_value={"stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1"},
    ):
        r = auth_client.post(f"{API}/billing/checkout", json={"plan": "weekly"})

    assert r.status_code == 200
    assert stripe_mock.checkout.Session.create.call_args.kwargs["customer"] == "cus_1"


def test_checkout_not_blocked_when_stripe_cannot_be_asked(auth_client, stripe_mock, monkeypatch):
    # Unverifiable ≠ subscribed. Blocking here would lock out someone who needs to pay;
    # the reused customer still keeps both subscriptions inside one portal.
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    stripe_mock.Subscription.retrieve.side_effect = RuntimeError("stripe down")
    stripe_mock.checkout.Session.create.return_value.url = "https://checkout.stripe.com/c/x"
    with patch(
        "app.routers.billing._billing_row",
        return_value={"stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1"},
    ):
        r = auth_client.post(f"{API}/billing/checkout", json={"plan": "weekly"})
    assert r.status_code == 200


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
    billing_db_mock.claim_event.return_value = False  # claimed by an earlier delivery
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


def test_webhook_grant_period_end_falls_back_to_item(
    client, stripe_mock, billing_db_mock, monkeypatch
):
    # stripe-python v15 pins an API version with no top-level current_period_end —
    # it lives on the item. Live bug 2026-09-06: expiry stored NULL, get_tier
    # fail-closed the paying user back to free.
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    sub = {
        "id": "sub_1",
        "status": "active",
        "items": {"data": [{"price": {"id": "price_w"}, "current_period_end": 1800000000}]},
    }
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "checkout.session.completed",
        {"client_reference_id": "u1", "customer": "cus_1", "subscription": "sub_1"},
    )
    stripe_mock.Subscription.retrieve.return_value = sub

    r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 200
    args, _kwargs = billing_db_mock.grant.call_args
    assert args[1] == "pro"
    assert args[2] is not None and args[2].startswith("2027-")


def test_webhook_unknown_price_grants_nothing(client, stripe_mock, billing_db_mock, monkeypatch):
    # Someone else's product in the same Stripe account must never grant our tier.
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    monkeypatch.setitem(PLANS["monthly"], "price_id", "price_m")
    stripe_mock.Price.retrieve.side_effect = lambda pid: (
        {"id": pid, "product": "prod_theirs" if pid == "price_foreign" else "prod_hiredrop"}
    )
    sub = {**SUB_ACTIVE, "items": {"data": [{"price": {"id": "price_foreign"}}]}}
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "checkout.session.completed",
        {"client_reference_id": "u1", "customer": "cus_1", "subscription": "sub_1"},
    )
    stripe_mock.Subscription.retrieve.return_value = sub

    r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 200
    billing_db_mock.grant.assert_not_called()


def test_webhook_legacy_price_on_our_product_still_grants(
    client, stripe_mock, billing_db_mock, monkeypatch
):
    """Stripe Prices are IMMUTABLE, so repricing mints new ids and the env holds only the
    current pair (we already repriced once: $9/$29 → $12/$39). A subscriber grandfathered on
    an older price fell through tier_for_price and was granted NOTHING — Stripe kept charging
    them while get_tier fail-closed them to free at period end. Paying and locked out is the
    worst state we can put someone in, so an unknown price on OUR product still grants."""
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    monkeypatch.setitem(PLANS["monthly"], "price_id", "price_m")
    # price_legacy_9 is gone from the env but sits on the same product as price_w.
    stripe_mock.Price.retrieve.side_effect = lambda pid: {"id": pid, "product": "prod_hiredrop"}
    sub = {**SUB_ACTIVE, "items": {"data": [{"price": {"id": "price_legacy_9"}}]}}
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "checkout.session.completed",
        {"client_reference_id": "u1", "customer": "cus_1", "subscription": "sub_1"},
    )
    stripe_mock.Subscription.retrieve.return_value = sub

    r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 200
    assert billing_db_mock.grant.call_args.args[1] == "pro"


def test_webhook_unknown_price_grants_nothing_when_stripe_cannot_answer(
    client, stripe_mock, billing_db_mock, monkeypatch
):
    # No answer from Stripe = no evidence it is ours. Stay closed.
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    stripe_mock.Price.retrieve.side_effect = RuntimeError("stripe down")
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "checkout.session.completed",
        {"client_reference_id": "u1", "customer": "cus_1", "subscription": "sub_1"},
    )
    stripe_mock.Subscription.retrieve.return_value = {
        **SUB_ACTIVE,
        "items": {"data": [{"price": {"id": "price_mystery"}}]},
    }

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


def test_webhook_unknown_customer_asks_for_re_delivery(client, stripe_mock, billing_db_mock):
    # "No user for this customer" is usually ORDER, not a stranger: the event overtook
    # the checkout that writes stripe_customer_id. Acking it (the old behaviour) burned
    # the event — Stripe never retries a 200 and a manual resend dedups.
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "customer.subscription.deleted", {"id": "sub_1", "customer": "cus_unknown"}
    )
    billing_db_mock.find_user_by_customer.return_value = None

    r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 503
    assert r.json()["error"] == "unresolved_customer"
    billing_db_mock.release_event.assert_called_once_with("evt_1")
    billing_db_mock.downgrade.assert_not_called()


def test_webhook_stops_asking_once_retries_cannot_help(client, stripe_mock, billing_db_mock):
    # After the window a customer really is foreign; keep the claim so Stripe stops
    # re-delivering (and stops alerting on a failing endpoint).
    from datetime import UTC, datetime, timedelta

    old_ts = int((datetime.now(tz=UTC) - timedelta(days=3)).timestamp())
    ev = make_event("invoice.paid", {"customer": "cus_stranger", "id": "in_1"})
    ev["created"] = old_ts
    stripe_mock.Webhook.construct_event.return_value = ev
    billing_db_mock.find_user_by_customer.return_value = None

    r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 200
    assert r.json()["unresolved"] is True
    billing_db_mock.release_event.assert_not_called()


def test_webhook_first_invoice_survives_arriving_before_checkout(
    client, stripe_mock, billing_db_mock
):
    """The money case this policy exists for: invoice.paid lands before
    checkout.session.completed has linked the customer, so the user can't be resolved.
    The old code marked the event done and returned 200 — the FIRST invoice's affiliate
    commission was gone, and no second invoice arrives inside the 60-day attribution
    window to replace it."""
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "invoice.paid", {"customer": "cus_1", "id": "in_1", "amount_paid": 900}
    )
    billing_db_mock.find_user_by_customer.return_value = None

    with patch("app.routers.billing.affiliates_db") as aff:
        r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 503  # → Stripe re-delivers, by then the link exists
    billing_db_mock.release_event.assert_called_once_with("evt_1")
    aff.accrue_from_invoice.assert_not_called()


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


def test_webhook_handler_error_returns_5xx_and_releases_the_claim(
    client, stripe_mock, billing_db_mock
):
    # Inverted on purpose (audit 09-25). The old rule was "never 500 a webhook", which
    # meant Stripe was told everything was fine while the grant never happened: the payer
    # sat on `free` until someone edited the database. A 5xx buys ~3 days of retries, and
    # releasing the claim is what lets a retry actually redo the work.
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "checkout.session.completed",
        {"client_reference_id": "u1", "customer": "cus_1", "subscription": "sub_1"},
    )
    stripe_mock.Subscription.retrieve.side_effect = RuntimeError("boom")

    r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 500
    assert r.json()["error"] == "handler_failed"
    billing_db_mock.release_event.assert_called_once_with("evt_1")


def test_webhook_link_customer_failure_is_retried_not_swallowed(
    client, stripe_mock, billing_db_mock
):
    """The worst single failure in the whole money path: link_customer throws, so
    stripe_customer_id is never written and EVERY later event for that customer is
    unresolvable. It has to come back."""
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "checkout.session.completed",
        {"client_reference_id": "u1", "customer": "cus_1", "subscription": "sub_1"},
    )
    billing_db_mock.link_customer.side_effect = RuntimeError("supabase down")

    r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 500
    billing_db_mock.release_event.assert_called_once_with("evt_1")


def test_webhook_successful_event_keeps_its_claim(
    client, stripe_mock, billing_db_mock, monkeypatch
):
    # The other half of the invariant: a handled event must NOT be released, or the next
    # delivery of it would grant twice.
    monkeypatch.setitem(PLANS["weekly"], "price_id", "price_w")
    stripe_mock.Webhook.construct_event.return_value = make_event(
        "checkout.session.completed",
        {"client_reference_id": "u1", "customer": "cus_1", "subscription": "sub_1"},
    )
    stripe_mock.Subscription.retrieve.return_value = SUB_ACTIVE

    r = client.post(f"{API}/billing/webhook", content=b"{}")

    assert r.status_code == 200
    billing_db_mock.release_event.assert_not_called()
