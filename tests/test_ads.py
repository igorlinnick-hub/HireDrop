"""Paid-ads plumbing: channel classification, verdicts, Meta CAPI, spend ingest,
and the admin board's Ads + Funnel sections.

No network: httpx and Stripe are mocked, Supabase reads are patched at the
function the section calls.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.ads import attribution as attr_rules
from app.ads import meta_capi, meta_spend
from app.ads import verdict as v
from app.db import ad_spend as spend_db
from app.routers import admin

API = "/api/v1"
FROM_TS, TO_TS = "2026-09-01T00:00:00Z", "2026-09-30T23:59:59Z"
FROM_DAY, TO_DAY = "2026-09-01", "2026-09-30"

META_ATTR = {
    "utm_source": "facebook",
    "utm_medium": "paid_social",
    "utm_campaign": "c1",
    "utm_content": "ad_1",
    "fbp": "fb.1.1700000000000.123",
    "ua": "Mozilla/5.0 Test",
    "captured_at": "2026-09-10T12:00:00.000Z",
}


@pytest.fixture(autouse=True)
def _no_ads_env(monkeypatch):
    """Every test starts with ads plumbing unconfigured and the sync memo clear."""
    for key in (
        "META_PIXEL_ID",
        "META_CAPI_TOKEN",
        "META_TEST_EVENT_CODE",
        "META_GRAPH_VERSION",
        "META_ADS_TOKEN",
        "META_AD_ACCOUNT_ID",
        "ADS_INGEST_TOKEN",
        "ADS_CAC_CEILING_USD",
        "ADS_MONTHLY_BUDGET_USD",
    ):
        monkeypatch.delenv(key, raising=False)
    meta_spend._last_attempt.update(at=0.0, error=None)


@pytest.fixture
def capi_env(monkeypatch):
    monkeypatch.setenv("META_PIXEL_ID", "PIXEL1")
    monkeypatch.setenv("META_CAPI_TOKEN", "TOKEN1")


# ------------------------------------------------------------ channel classification


@pytest.mark.parametrize(
    ("attribution", "channel"),
    [
        ({"utm_source": "facebook", "utm_medium": "paid_social"}, "meta_paid"),
        ({"utm_source": "Instagram", "utm_medium": "CPC"}, "meta_paid"),
        ({"utm_source": "ig", "utm_medium": "paid"}, "meta_paid"),
        ({"utm_source": "meta", "utm_medium": "sponsored"}, "meta_paid"),
        # organic Meta: a post/bio link is not an ad
        ({"utm_source": "facebook", "utm_medium": "social"}, "facebook"),
        ({"utm_source": "instagram"}, "instagram"),
        # fbclid alone is NOT paid — Facebook stamps it on organic outbound links too
        ({"fbclid": "IwAR123"}, "direct"),
        ({"utm_source": "facebook", "fbclid": "IwAR123"}, "facebook"),
        ({"gclid": "Cj0KCQ"}, "google_paid"),
        ({"gbraid": "0AAAA"}, "google_paid"),
        ({"wbraid": "0BBBB"}, "google_paid"),
        ({"utm_source": "google", "utm_medium": "cpc"}, "google_paid"),
        ({"utm_source": "google", "utm_medium": "organic"}, "google"),
        ({"utm_source": "chatgpt.com"}, "chatgpt.com"),
        ({"ref": "luca", "utm_source": "tiktok"}, "ref:luca"),
        ({}, "direct"),
        (None, "direct"),
    ],
)
def test_channel_of(attribution, channel):
    assert attr_rules.channel_of(attribution) == channel


def test_blank_click_id_is_not_a_google_click():
    assert attr_rules.channel_of({"gclid": "  "}) == "direct"


def test_admin_source_of_still_answers_the_same():
    assert admin._source_of({"ref": "x"}) == "ref:x"
    assert admin._source_of({"utm_source": "chatgpt.com"}) == "chatgpt.com"
    assert admin._source_of(None) == "direct"


# ------------------------------------------------------------ verdict rules


def test_verdict_wait_until_there_is_something_to_judge():
    assert v.ad_verdict(15, 1500, 10, 0, 0, 0, 39) == v.WAIT


def test_verdict_wait_even_with_an_early_lucky_payer():
    assert v.ad_verdict(10, 500, 5, 1, 1, 1, 39) == v.WAIT


def test_verdict_kill_spend_without_signups():
    assert v.ad_verdict(30, 1500, 30, 0, 0, 0, 39) == v.KILL


def test_verdict_kill_low_ctr():
    # 2000 impressions, 10 clicks = 0.5% < 0.7%
    assert v.ad_verdict(19, 2000, 10, 1, 0, 0, 39) == v.KILL


def test_verdict_kill_signups_that_never_activate():
    assert v.ad_verdict(60, 1900, 40, 3, 0, 0, 39) == v.KILL


def test_verdict_scale_on_cheap_paying_user():
    assert v.ad_verdict(35, 3000, 40, 3, 1, 1, 39) == v.SCALE


def test_verdict_scale_on_cheap_activations():
    # $38 / 2 activated = $19 <= 39/2
    assert v.ad_verdict(38, 3000, 40, 4, 2, 0, 39) == v.SCALE


def test_verdict_money_beats_a_low_ctr():
    # CTR 0.5% would kill it, but it pays for itself.
    assert v.ad_verdict(30, 4000, 20, 2, 1, 1, 39) == v.SCALE


def test_verdict_keep_otherwise():
    assert v.ad_verdict(25, 1500, 30, 1, 0, 0, 39) == v.KEEP


def test_verdict_expensive_payer_is_not_scale():
    assert v.ad_verdict(50, 3000, 40, 2, 1, 1, 39) == v.KEEP


def test_cac_ceiling_env(monkeypatch):
    assert v.cac_ceiling() == 39.0
    monkeypatch.setenv("ADS_CAC_CEILING_USD", "12")
    assert v.cac_ceiling() == 12.0
    monkeypatch.setenv("ADS_CAC_CEILING_USD", "nope")
    assert v.cac_ceiling() == 39.0


def test_ctr_unknown_without_clicks():
    assert v.ctr_pct(None, 1000) is None
    assert v.ctr_pct(5, 0) is None
    assert v.ctr_pct(7, 1000) == 0.7


# ------------------------------------------------------------ Meta CAPI: gate + payload


def test_capi_gate():
    assert meta_capi.should_send(META_ATTR)
    assert meta_capi.should_send({"fbc": "fb.1.1.abc"})
    assert meta_capi.should_send({"fbclid": "abc"})
    assert not meta_capi.should_send({"utm_source": "facebook", "utm_medium": "social"})
    assert not meta_capi.should_send({"gclid": "x"})
    assert not meta_capi.should_send(None)
    assert not meta_capi.should_send({**META_ATTR, "ads_optout": True})
    assert not meta_capi.should_send({**META_ATTR, "ads_optout": "true"})
    assert meta_capi.should_send({**META_ATTR, "ads_optout": False})


def test_fbc_prefers_the_cookie():
    assert meta_capi.build_fbc({"fbc": "fb.1.99.cookie", "fbclid": "x"}) == "fb.1.99.cookie"


def test_fbc_rebuilt_from_fbclid_and_capture_time():
    fbc = meta_capi.build_fbc({"fbclid": "IwAR1", "captured_at": "2026-09-10T12:00:00.000Z"})
    assert fbc == "fb.1.1789041600000.IwAR1"


def test_fbc_none_without_capture_time():
    assert meta_capi.build_fbc({"fbclid": "IwAR1"}) is None
    assert meta_capi.build_fbc({}) is None


def test_event_payload_shape():
    event = meta_capi.build_event(
        "StartTrial",
        "act_u1",
        "u1",
        "  Jane@Example.COM ",
        {**META_ATTR, "fbclid": "IwAR1"},
        event_time=1790000000,
    )
    assert event["event_name"] == "StartTrial"
    assert event["event_id"] == "act_u1"
    assert event["event_time"] == 1790000000
    assert event["action_source"] == "website"
    assert event["event_source_url"] == "https://hiredrop.io/dashboard"
    ud = event["user_data"]
    assert ud["em"] == [meta_capi.sha256("jane@example.com")]
    assert ud["external_id"] == [meta_capi.sha256("u1")]
    assert ud["fbp"] == META_ATTR["fbp"]
    assert ud["fbc"].startswith("fb.1.") and ud["fbc"].endswith(".IwAR1")
    assert ud["client_user_agent"] == "Mozilla/5.0 Test"
    # minimisation: nothing else about the person
    assert set(ud) == {"em", "external_id", "fbp", "fbc", "client_user_agent"}
    assert "custom_data" not in event


def test_send_posts_to_the_pixel_with_test_code(capi_env, monkeypatch):
    monkeypatch.setenv("META_TEST_EVENT_CODE", "TEST123")
    ok = MagicMock(status_code=200, text="{}")
    with patch("app.ads.meta_capi.httpx.post", return_value=ok) as post:
        assert meta_capi.send([{"event_name": "StartTrial", "event_id": "act_u1"}]) is True
    url = post.call_args.args[0]
    kwargs = post.call_args.kwargs
    assert url == "https://graph.facebook.com/v24.0/PIXEL1/events"
    assert kwargs["params"] == {"access_token": "TOKEN1"}
    assert kwargs["json"]["test_event_code"] == "TEST123"
    assert kwargs["timeout"] == 5.0


def test_send_never_raises(capi_env):
    with patch("app.ads.meta_capi.httpx.post", side_effect=RuntimeError("down")):
        assert meta_capi.send([{"event_name": "X", "event_id": "y"}]) is False
    bad = MagicMock(status_code=400, text='{"error":"bad"}')
    with patch("app.ads.meta_capi.httpx.post", return_value=bad):
        assert meta_capi.send([{"event_name": "X", "event_id": "y"}]) is False


def test_unset_env_is_a_complete_no_op():
    assert meta_capi.config() is None
    with (
        patch("app.ads.meta_capi._fire") as fire,
        patch("app.ads.meta_capi.httpx.post") as post,
    ):
        meta_capi.track_start_trial("u1", "a@b.c")
        meta_capi.track_purchase("u1", {"id": "in_1", "amount_paid": 1200})
    fire.assert_not_called()
    post.assert_not_called()


def _sync_fire():
    """Run the background job inline so the test sees its effect."""
    return patch("app.ads.meta_capi._fire", side_effect=lambda job, *a: meta_capi._run(job, *a))


def test_start_trial_sent_on_first_application_only(capi_env):
    with (
        _sync_fire(),
        patch("app.ads.meta_capi._attribution", return_value=META_ATTR),
        patch("app.ads.meta_capi._is_first_application", return_value=True),
        patch("app.ads.meta_capi.send") as send,
    ):
        meta_capi.track_start_trial("u1", "a@b.c")
    (events,) = send.call_args.args
    assert events[0]["event_name"] == "StartTrial"
    assert events[0]["event_id"] == "act_u1"

    with (
        _sync_fire(),
        patch("app.ads.meta_capi._attribution", return_value=META_ATTR),
        patch("app.ads.meta_capi._is_first_application", return_value=False),
        patch("app.ads.meta_capi.send") as send,
    ):
        meta_capi.track_start_trial("u1", "a@b.c")
    send.assert_not_called()


def test_start_trial_not_sent_for_non_meta_or_opted_out(capi_env):
    for attribution in ({"utm_source": "chatgpt.com"}, {**META_ATTR, "ads_optout": True}, None):
        with (
            _sync_fire(),
            patch("app.ads.meta_capi._attribution", return_value=attribution),
            patch("app.ads.meta_capi._is_first_application", return_value=True) as first,
            patch("app.ads.meta_capi.send") as send,
        ):
            meta_capi.track_start_trial("u1", "a@b.c")
        send.assert_not_called()
        first.assert_not_called()  # gate first: no applications read for non-Meta users


def test_meta_user_without_user_agent_is_skipped(capi_env):
    attribution = {k: val for k, val in META_ATTR.items() if k != "ua"}
    with (
        _sync_fire(),
        patch("app.ads.meta_capi._attribution", return_value=attribution),
        patch("app.ads.meta_capi._is_first_application", return_value=True),
        patch("app.ads.meta_capi.send") as send,
    ):
        meta_capi.track_start_trial("u1", "a@b.c")
    send.assert_not_called()


def test_purchase_payload(capi_env):
    invoice = {
        "id": "in_42",
        "amount_paid": 3900,
        "currency": "usd",
        "customer_email": "x@y.z",
        "status_transitions": {"paid_at": 1790000000},
    }
    with (
        _sync_fire(),
        patch("app.ads.meta_capi._attribution", return_value=META_ATTR),
        patch("app.ads.meta_capi._email_for", return_value="Owner@Mail.com"),
        patch("app.ads.meta_capi.send") as send,
    ):
        meta_capi.track_purchase("u1", invoice)
    (event,) = send.call_args.args[0]
    assert event["event_name"] == "Purchase"
    assert event["event_id"] == "pay_in_42"
    assert event["event_time"] == 1790000000
    assert event["custom_data"] == {"value": 39.0, "currency": "USD"}
    assert event["user_data"]["em"] == [meta_capi.sha256("owner@mail.com")]


def test_zero_dollar_invoice_is_not_a_purchase(capi_env):
    with patch("app.ads.meta_capi._fire") as fire:
        meta_capi.track_purchase("u1", {"id": "in_0", "amount_paid": 0})
    fire.assert_not_called()


def test_worker_failure_is_swallowed(capi_env):
    with (
        _sync_fire(),
        patch("app.ads.meta_capi._attribution", side_effect=RuntimeError("db down")),
    ):
        meta_capi.track_start_trial("u1")  # must not raise
    with patch("app.ads.meta_capi.threading.Thread", side_effect=RuntimeError("no threads")):
        meta_capi.track_start_trial("u1")  # scheduling failure must not raise either


# ------------------------------------------------------------ hooks


def test_application_save_fires_start_trial(auth_client, fake_user):
    check = {
        "allowed": True,
        "reason": "",
        "tier": "pro",
        "used_today": 0,
        "daily_limit": 30,
        "free_used": None,
        "free_limit": None,
    }
    with (
        patch("app.routers.applications.check_can_apply", return_value=check),
        patch("app.routers.applications.jobs_db.save_job", return_value="job-1"),
        patch("app.routers.applications.jobs_db.mark_applied_by_link"),
        patch("app.routers.applications.apps_db.save_application"),
        patch("app.routers.applications.meta_capi.track_start_trial") as track,
    ):
        res = auth_client.post(
            f"{API}/applications/save", json={"job_title": "Dev", "company": "Acme"}
        )
    assert res.status_code == 200
    track.assert_called_once_with(fake_user.id, fake_user.email)


def test_denied_save_fires_nothing(auth_client):
    check = {
        "allowed": False,
        "reason": "limit",
        "tier": "free",
        "used_today": 0,
        "daily_limit": 20,
        "free_used": 40,
        "free_limit": 40,
    }
    with (
        patch("app.routers.applications.check_can_apply", return_value=check),
        patch("app.routers.applications.meta_capi.track_start_trial") as track,
    ):
        res = auth_client.post(
            f"{API}/applications/save", json={"job_title": "Dev", "company": "Acme"}
        )
    assert res.status_code == 429
    track.assert_not_called()


@pytest.fixture
def webhook(monkeypatch):
    stripe = MagicMock()
    billing = MagicMock()
    billing.claim_event.return_value = True
    billing.find_user_by_customer.return_value = "u1"
    with (
        patch("app.routers.billing._stripe", return_value=stripe),
        patch("app.routers.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"),
        patch("app.routers.billing.billing_db", billing),
        patch("app.routers.billing._resolve_subscription", return_value=None),
        patch("app.routers.billing.affiliates_db") as aff,
    ):
        yield stripe, billing, aff


def _invoice_event():
    return {
        "id": "evt_9",
        "type": "invoice.paid",
        "data": {
            "object": {"id": "in_9", "customer": "cus_1", "amount_paid": 1200, "currency": "usd"}
        },
    }


def test_invoice_paid_sends_purchase_after_accrual(client, webhook):
    stripe, billing, aff = webhook
    stripe.Webhook.construct_event.return_value = _invoice_event()
    order = MagicMock()
    aff.accrue_from_invoice.side_effect = lambda *a: order("accrue")
    with patch(
        "app.routers.billing.meta_capi.track_purchase",
        side_effect=lambda *a: order("purchase"),
    ) as track:
        r = client.post(f"{API}/billing/webhook", content=b"{}")
    assert r.status_code == 200
    assert [c.args[0] for c in order.call_args_list] == ["accrue", "purchase"]
    assert track.call_args.args[0] == "u1"
    assert track.call_args.args[1]["id"] == "in_9"


def test_capi_failure_never_becomes_a_5xx(client, webhook, capi_env):
    """The whole real path, run inline: Meta down must leave the webhook at 200
    and the claim in place (no release = no Stripe retry storm)."""
    stripe, billing, _aff = webhook
    stripe.Webhook.construct_event.return_value = _invoice_event()
    with (
        _sync_fire(),
        patch("app.ads.meta_capi._attribution", return_value=META_ATTR),
        patch("app.ads.meta_capi._email_for", return_value="a@b.c"),
        patch("app.ads.meta_capi.httpx.post", side_effect=RuntimeError("graph down")) as post,
    ):
        r = client.post(f"{API}/billing/webhook", content=b"{}")
    assert r.status_code == 200
    post.assert_called_once()
    billing.release_event.assert_not_called()


# ------------------------------------------------------------ spend ingest endpoint

GOOD_BODY = {
    "platform": "google",
    "account_id": "123-456-7890",
    "rows": [
        {
            "date": "2026-09-27",
            "campaign_id": "111",
            "campaign_name": "Search US",
            "ad_id": "999",
            "ad_name": None,
            "spend_usd": 12.345,
            "impressions": 800,
            "clicks": 20,
        }
    ],
}


def test_ingest_503_when_unconfigured(client):
    r = client.post(f"{API}/admin/ads/spend", json=GOOD_BODY, headers={"X-Ads-Ingest-Token": "x"})
    assert r.status_code == 503


def test_ingest_401_on_wrong_or_missing_token(client, monkeypatch):
    monkeypatch.setenv("ADS_INGEST_TOKEN", "right")
    r = client.post(f"{API}/admin/ads/spend", json=GOOD_BODY, headers={"X-Ads-Ingest-Token": "no"})
    assert r.status_code == 401
    r = client.post(f"{API}/admin/ads/spend", json=GOOD_BODY)
    assert r.status_code == 401


def test_ingest_checks_the_token_before_the_body(client, monkeypatch):
    monkeypatch.setenv("ADS_INGEST_TOKEN", "right")
    r = client.post(f"{API}/admin/ads/spend", json={"nonsense": True})
    assert r.status_code == 401


def test_ingest_upserts(client, monkeypatch):
    monkeypatch.setenv("ADS_INGEST_TOKEN", "right")
    with patch("app.routers.ads.spend_db.upsert", return_value=1) as upsert:
        r = client.post(
            f"{API}/admin/ads/spend", json=GOOD_BODY, headers={"X-Ads-Ingest-Token": "right"}
        )
    assert r.status_code == 200
    assert r.json() == {"upserted": 1, "received": 1}
    (rows,) = upsert.call_args.args
    assert rows[0] == {
        "date": "2026-09-27",
        "platform": "google",
        "account_id": "123-456-7890",
        "campaign_id": "111",
        "campaign_name": "Search US",
        "ad_id": "999",
        "ad_name": None,
        "spend_usd": 12.35,
        "impressions": 800,
        "clicks": 20,
        "source": "google_ingest",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(platform="meta"),  # Meta has one writer: the Insights sync
        lambda b: b["rows"][0].update(spend_usd=-1),
        lambda b: b["rows"][0].update(date="yesterday"),
        lambda b: b["rows"][0].update(impressions=-5),
        lambda b: b["rows"][0].update(ad_id=None, campaign_id=None, campaign_name=None),
    ],
)
def test_ingest_validation(client, monkeypatch, mutate):
    import copy

    monkeypatch.setenv("ADS_INGEST_TOKEN", "right")
    body = copy.deepcopy(GOOD_BODY)
    mutate(body)
    with patch("app.routers.ads.spend_db.upsert") as upsert:
        r = client.post(
            f"{API}/admin/ads/spend", json=body, headers={"X-Ads-Ingest-Token": "right"}
        )
    assert r.status_code == 422
    upsert.assert_not_called()


def test_ingest_falls_back_to_a_campaign_key(client, monkeypatch):
    monkeypatch.setenv("ADS_INGEST_TOKEN", "right")
    body = {
        "platform": "manual",
        "rows": [{"date": "2026-09-27", "campaign_name": "Flyers", "spend_usd": 5}],
    }
    with patch("app.routers.ads.spend_db.upsert", return_value=1) as upsert:
        r = client.post(
            f"{API}/admin/ads/spend", json=body, headers={"X-Ads-Ingest-Token": "right"}
        )
    assert r.status_code == 200
    assert upsert.call_args.args[0][0]["ad_id"] == "campaign:Flyers"


def test_duplicate_keys_in_one_batch_are_summed():
    rows = [
        {
            "platform": "google",
            "date": "2026-09-27",
            "ad_id": "9",
            "spend_usd": 1.1,
            "impressions": 10,
            "clicks": 1,
        },
        {
            "platform": "google",
            "date": "2026-09-27",
            "ad_id": "9",
            "spend_usd": 2.2,
            "impressions": 5,
            "clicks": None,
        },
        {
            "platform": "google",
            "date": "2026-09-28",
            "ad_id": "9",
            "spend_usd": 3.0,
            "impressions": 1,
            "clicks": 0,
        },
    ]
    merged = spend_db.merge_duplicates(rows)
    assert len(merged) == 2
    first = next(r for r in merged if r["date"] == "2026-09-27")
    assert first["spend_usd"] == 3.3
    assert first["impressions"] == 15
    assert first["clicks"] == 1


# ------------------------------------------------------------ Meta spend sync


def test_insights_rows_map_to_ad_spend():
    rows = meta_spend.to_rows(
        [
            {
                "date_start": "2026-09-27",
                "campaign_id": "c1",
                "campaign_name": "Launch",
                "adset_id": "as1",
                "ad_id": "ad_1",
                "ad_name": "Hook A",
                "spend": "12.345",
                "impressions": "1500",
                "inline_link_clicks": "22",
                "account_currency": "USD",
            }
        ],
        "777",
    )
    assert rows == [
        {
            "date": "2026-09-27",
            "platform": "meta",
            "account_id": "777",
            "campaign_id": "c1",
            "campaign_name": "Launch",
            "adset_id": "as1",
            "ad_id": "ad_1",
            "ad_name": "Hook A",
            "spend_usd": 12.35,
            "impressions": 1500,
            "clicks": 22,
            "source": "meta_insights",
        }
    ]


def test_non_usd_account_refuses_to_store():
    with pytest.raises(meta_spend.MetaSpendError):
        meta_spend.to_rows(
            [{"date_start": "2026-09-27", "ad_id": "a", "account_currency": "EUR"}], "1"
        )


def test_insights_paginates(monkeypatch):
    monkeypatch.setenv("META_ADS_TOKEN", "t")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_777")
    page1 = MagicMock(status_code=200)
    page1.json.return_value = {"data": [{"ad_id": "1"}], "paging": {"next": "https://graph/next"}}
    page2 = MagicMock(status_code=200)
    page2.json.return_value = {"data": [{"ad_id": "2"}], "paging": {}}
    from datetime import date

    with patch("app.ads.meta_spend.httpx.get", side_effect=[page1, page2]) as get:
        out = meta_spend.fetch_insights(meta_spend.config(), date(2026, 9, 1), date(2026, 9, 7))
    assert [r["ad_id"] for r in out] == ["1", "2"]
    first_url = get.call_args_list[0].args[0]
    assert first_url.endswith("/act_777/insights")  # act_ prefix not doubled
    assert get.call_args_list[1].args[0] == "https://graph/next"


def test_sync_if_stale_not_connected():
    state = meta_spend.sync_if_stale()
    assert state["connected"] is False
    assert state["error"] == meta_spend.NOT_CONNECTED


def test_sync_if_stale_skips_fresh_data(monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setenv("META_ADS_TOKEN", "t")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "1")
    with (
        patch(
            "app.ads.meta_spend.spend_db.newest_synced_at",
            return_value=datetime.now(UTC).isoformat(),
        ),
        patch("app.ads.meta_spend.sync") as sync,
    ):
        state = meta_spend.sync_if_stale()
    sync.assert_not_called()
    assert state["ran"] is False and state["error"] is None


def test_sync_if_stale_runs_and_names_a_failure_once_per_hour(monkeypatch):
    monkeypatch.setenv("META_ADS_TOKEN", "t")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "1")
    with (
        patch("app.ads.meta_spend.spend_db.newest_synced_at", return_value=None),
        patch(
            "app.ads.meta_spend.sync", side_effect=meta_spend.MetaSpendError("token expired")
        ) as sync,
    ):
        state = meta_spend.sync_if_stale()
        again = meta_spend.sync_if_stale()
    assert sync.call_count == 1  # the memo stops a Meta call on every board load
    assert "token expired" in state["error"]
    assert "token expired" in again["error"]


# ------------------------------------------------------------ Funnel section


def _profile(uid, created="2026-09-10T00:00:00Z", **extra):
    return {"user_id": uid, "created_at": created, "onboarding_completed": False, **extra}


def test_funnel_counts_people_not_keys():
    profiles = [
        _profile("a", onboarding_completed=True),
        _profile("b"),
        _profile("c"),
        _profile("old", "2026-08-01T00:00:00Z"),
    ]
    keys = [{"user_id": "a"}] * 6 + [
        {"user_id": "old"}
    ] * 3  # re-issued keys, plus a pre-period user
    apps = [{"user_id": "a", "date_applied": "2026-09-12T00:00:00Z"}] * 4 + [
        {"user_id": "old", "date_applied": "2026-09-12T00:00:00Z"}
    ]
    with (
        patch("app.routers.admin._paged", return_value=keys),
        patch("app.routers.admin._count", return_value=0),
    ):
        section = admin._section_funnel(profiles, apps, FROM_TS, TO_TS)
    steps = {r["step"]: r for r in section["tables"][0]["rows"]}
    assert steps["Signed up"]["count"] == 3
    assert steps["Connected extension"]["count"] == 1
    assert steps["Connected extension"]["of_signups"] == 33.3
    assert steps["Sent first application"]["count"] == 1
    assert all((r["of_signups"] or 0) <= 100 for r in steps.values())
    metrics = {m["key"]: m for m in section["metrics"]}
    assert metrics["activation_rate"]["value"] == 33.3
    assert metrics["first_application"]["value"] == 1
    assert "campaigns_started" not in metrics


def test_funnel_unreadable_keys_is_unknown_not_zero():
    with (
        patch("app.routers.admin._paged", side_effect=RuntimeError("boom")),
        patch("app.routers.admin._count", return_value=0),
    ):
        section = admin._section_funnel([_profile("a")], [], FROM_TS, TO_TS)
    steps = {r["step"]: r for r in section["tables"][0]["rows"]}
    assert steps["Connected extension"]["count"] is None
    assert steps["Connected extension"]["of_signups"] is None


# ------------------------------------------------------------ Ads section


def _ads_section(profiles, apps=(), spend_rows=(), meta_state=None, stripe=None):
    meta_state = meta_state or {
        "connected": False,
        "ran": False,
        "rows": None,
        "error": meta_spend.NOT_CONNECTED,
        "newest_synced_at": None,
    }
    with (
        patch("app.routers.admin.meta_spend.sync_if_stale", return_value=meta_state),
        patch("app.routers.admin.spend_db.read_all", return_value=list(spend_rows)),
        patch("app.routers.admin._stripe_client", return_value=stripe),
    ):
        return admin._section_ads(list(profiles), list(apps), FROM_TS, TO_TS, FROM_DAY, TO_DAY)


def _metrics(section):
    return {m["key"]: m for m in section["metrics"]}


def _table_rows(section, key):
    return next(t for t in section["tables"] if t["key"] == key)["rows"]


def test_ads_with_no_spend_source_says_why_instead_of_zero():
    profiles = [
        _profile("m1", attribution=META_ATTR),
        _profile("o1", attribution={"utm_source": "chatgpt.com"}),
    ]
    section = _ads_section(profiles, apps=[{"user_id": "m1"}])
    m = _metrics(section)
    assert m["spend"]["value"] is None
    assert "META_ADS_TOKEN" in m["spend"]["description"]
    for key in ("budget_used", "cost_per_signup", "cost_per_activation", "cac", "roas"):
        assert m[key]["value"] is None, key
        assert m[key]["description"], key
    assert m["paid_signups"]["value"] == 1
    assert m["activated_from_ads"]["value"] == 1
    channels = {r["channel"]: r for r in _table_rows(section, "by_channel")}
    assert channels["meta_paid"]["spend"] is None
    assert channels["chatgpt.com"]["spend"] is None
    assert channels["chatgpt.com"]["signups"] == 1


def test_ads_unreadable_table_is_named():
    with (
        patch(
            "app.routers.admin.meta_spend.sync_if_stale",
            return_value={"connected": False, "error": "x"},
        ),
        patch("app.routers.admin.spend_db.read_all", side_effect=RuntimeError("relation missing")),
        patch("app.routers.admin._stripe_client", return_value=None),
    ):
        section = admin._section_ads([], [], FROM_TS, TO_TS, FROM_DAY, TO_DAY)
    m = _metrics(section)
    assert m["spend"]["value"] is None
    assert "ad_spend unreadable" in m["spend"]["description"]


def _meta_row(ad_id, date, spend, impressions=1000, clicks=20, name=None):
    return {
        "platform": "meta",
        "date": date,
        "campaign_id": "c1",
        "campaign_name": "Launch",
        "ad_id": ad_id,
        "ad_name": name,
        "spend_usd": spend,
        "impressions": impressions,
        "clicks": clicks,
        "synced_at": "2026-09-28T10:00:00+00:00",
        "account_id": "777",
    }


def _stripe_with(charges):
    stripe = MagicMock()
    stripe.Charge.list.return_value.auto_paging_iter.return_value = charges
    return stripe


def test_ads_joins_spend_to_signups_by_ad():
    connected = {
        "connected": True,
        "ran": False,
        "rows": None,
        "error": None,
        "newest_synced_at": "x",
    }
    spend = [
        _meta_row("ad_1", "2026-09-10", 20.0, 1500, 30, name="Hook A"),
        _meta_row("ad_1", "2026-09-11", 15.0, 1500, 30, name="Hook A"),
        _meta_row("ad_2", "2026-09-11", 31.0, 1000, 20),
        _meta_row("ad_1", "2026-08-20", 99.0),  # before the period: not counted
    ]
    profiles = [
        _profile("m1", attribution=META_ATTR, stripe_customer_id="cus_1"),
        _profile("m2", attribution=META_ATTR),
        _profile("m3", attribution={**META_ATTR, "utm_content": "ad_unknown"}),
        _profile("m4", attribution={"utm_source": "fb", "utm_medium": "cpc"}),  # no utm_content
        _profile("g1", attribution={"gclid": "abc", "utm_content": "555"}),
        _profile("o1", attribution={"utm_source": "chatgpt.com"}),
    ]
    apps = [{"user_id": "m1"}, {"user_id": "m2"}]
    stripe = _stripe_with(
        [
            {
                "paid": True,
                "status": "succeeded",
                "customer": "cus_1",
                "amount": 3900,
                "amount_refunded": 0,
                "created": 1789000000,
            }
        ]
    )
    section = _ads_section(profiles, apps, spend, connected, stripe)
    m = _metrics(section)

    assert m["spend"]["value"] == 66.0
    assert m["paid_signups"]["value"] == 5
    assert m["activated_from_ads"]["value"] == 2
    assert m["paying_from_ads"]["value"] == 1
    assert m["cac"]["value"] == 66.0
    assert m["cost_per_signup"]["value"] == 13.2
    assert m["roas"]["value"] == round(39.0 / 66.0, 2)

    ads = {r["ad"]: r for r in _table_rows(section, "by_ad")}
    hook = ads["Hook A"]
    assert hook["spend"] == 35.0
    assert hook["impressions"] == 3000
    assert hook["ctr"] == 2.0
    assert hook["signups"] == 2 and hook["activated"] == 2 and hook["paid"] == 1
    assert hook["verdict"] == v.SCALE  # 1 payer at $35 <= $39
    no_name = ads["ad_2"]  # name falls back to the id
    assert no_name["signups"] == 0 and no_name["verdict"] == v.KILL  # $31, no signups

    unmatched = {r["account"]: r for r in _table_rows(section, "unmatched")}
    assert set(unmatched) == {"m3", "m4", "g1"}
    assert "no utm_content" in unmatched["m4"]["why"]
    assert "not connected" in unmatched["g1"]["why"]  # Google has never posted

    channels = {r["channel"]: r for r in _table_rows(section, "by_channel")}
    assert channels["meta_paid"]["spend"] == 66.0
    assert channels["google_paid"]["spend"] is None  # not connected: unknown, not $0
    assert channels["google_paid"]["signups"] == 1
    assert channels["chatgpt.com"]["spend"] is None

    assert [p["value"] for p in section["timeseries"]["points"]] == [20.0, 46.0]


def test_manual_spend_is_matched_to_its_utm_source():
    spend = [
        {
            "platform": "manual",
            "date": "2026-09-12",
            "account_id": "reddit",
            "ad_id": "manual:thread",
            "campaign_name": "thread",
            "spend_usd": 25,
            "impressions": None,
            "clicks": None,
            "synced_at": "2026-09-12T00:00:00+00:00",
        }
    ]
    profiles = [_profile("r1", attribution={"utm_source": "Reddit"}), _profile("o1")]
    section = _ads_section(profiles, [{"user_id": "r1"}], spend)
    m = _metrics(section)
    assert m["spend"]["value"] == 25.0
    assert m["paid_signups"]["value"] == 1
    assert m["cost_per_activation"]["value"] == 25.0
    channels = {r["channel"]: r for r in _table_rows(section, "by_channel")}
    assert channels["reddit"]["spend"] == 25.0
    assert channels["direct"]["spend"] is None
    assert _table_rows(section, "by_ad") == []  # manual spend has no ad-level join


def test_paid_signal_nets_out_refunds_and_falls_back_without_stripe():
    profiles = [
        _profile("p1", attribution=META_ATTR, stripe_customer_id="cus_1"),
        _profile("p2", attribution=META_ATTR, stripe_customer_id="cus_2"),
    ]
    charges = [
        {
            "paid": True,
            "status": "succeeded",
            "customer": "cus_1",
            "amount": 1200,
            "amount_refunded": 0,
            "created": 1,
        },
        {
            "paid": True,
            "status": "succeeded",
            "customer": "cus_2",
            "amount": 1200,
            "amount_refunded": 1200,
            "created": 1,
        },
        {
            "paid": False,
            "status": "failed",
            "customer": "cus_2",
            "amount": 1200,
            "amount_refunded": 0,
            "created": 1,
        },
    ]
    with patch("app.routers.admin._stripe_client", return_value=_stripe_with(charges)):
        paid, how = admin._paid_users(profiles, FROM_TS)
    assert paid == {"p1": 1200}
    assert "Stripe" in how

    broken = MagicMock()
    broken.Charge.list.side_effect = RuntimeError("stripe down")
    with patch("app.routers.admin._stripe_client", return_value=broken):
        paid, how = admin._paid_users(profiles, FROM_TS)
    assert paid is None
    assert "linked Stripe customer" in how

    section = _ads_section(profiles, stripe=broken)
    m = _metrics(section)
    assert m["paying_from_ads"]["value"] == 2  # fallback: both completed a checkout
    assert m["roas"]["value"] is None


def test_budget_gauge(monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setenv("ADS_MONTHLY_BUDGET_USD", "300")
    today = datetime.now(UTC).date().isoformat()
    connected = {
        "connected": True,
        "ran": False,
        "rows": None,
        "error": None,
        "newest_synced_at": "x",
    }
    section = _ads_section([], [], [_meta_row("ad_1", today, 75.0)], connected)
    m = _metrics(section)
    assert m["budget_used"]["value"] == 25.0
    assert "$75.00 of $300" in m["budget_used"]["description"]
