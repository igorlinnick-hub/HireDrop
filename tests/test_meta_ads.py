"""scripts/meta_ads.py — the payloads that create the Meta campaign.

What must never drift, because each one costs money or the account if it does:
  - every object is created PAUSED (launching is Igor's click, never the script's);
  - the campaign carries the EMPLOYMENT special ad category (Meta restricts
    accounts that run job-related ads without it);
  - the ad set optimises for the pixel's CompleteRegistration — the event the
    site fires as `reg_<user_id>` — and leaves placements to Advantage+;
  - every creative carries the UTM template the Ads tab joins spend on;
  - a 4:5 + 9:16 pair becomes one ad with placement rules, 9:16 winning
    Stories/Reels.
"""

import json

import pytest

from scripts import meta_ads as m

SPEC = {
    "account_id": "1672047784439131",
    "pixel_id": "1113284891046203",
    "page_id": "111",
    "link": "https://hiredrop.io/",
    "url_tags": "utm_source=facebook&utm_medium=paid_social&utm_content={{ad.id}}",
    "campaign": {"name": "C", "daily_budget_usd": 8},
    "adset": {"name": "S"},
    "ads": [],
}
AD = {"key": "M2", "name": "M2 · static", "primary": "P", "headline": "H"}


def test_campaign_is_paused_employment_and_budget_in_cents():
    p = m.campaign_payload(SPEC)
    assert p["status"] == "PAUSED"
    assert p["special_ad_categories"] == ["EMPLOYMENT"]
    assert p["special_ad_category_country"] == ["US"]
    assert p["daily_budget"] == 800
    assert p["objective"] == "OUTCOME_LEADS"


def test_adset_optimises_for_registration_with_advantage_placements():
    p = m.adset_payload(SPEC, "c1")
    assert p["status"] == "PAUSED"
    assert p["campaign_id"] == "c1"
    assert p["promoted_object"] == {"pixel_id": "1113284891046203", "custom_event_type": "COMPLETE_REGISTRATION"}
    assert p["optimization_goal"] == "OFFSITE_CONVERSIONS"
    assert p["targeting"]["geo_locations"] == {"countries": ["US"]}
    # Employment category: no age/gender/ZIP narrowing, and no hand-picked placements.
    assert not {"age_min", "age_max", "genders", "zips", "publisher_platforms"} & set(p["targeting"])


def test_image_pair_becomes_one_ad_with_vertical_rule_first():
    c = m.creative_payload(SPEC, AD, {"feed_image": "h45", "story_image": "h916"})
    assert c["url_tags"] == SPEC["url_tags"]
    feed = c["asset_feed_spec"]
    assert feed["optimization_type"] == "PLACEMENT"
    assert feed["call_to_action_types"] == ["SIGN_UP"]
    assert feed["link_urls"] == [{"website_url": "https://hiredrop.io/"}]
    by_label = {i["adlabels"][0]["name"]: i["hash"] for i in feed["images"]}
    assert by_label == {"m2_feed": "h45", "m2_vertical": "h916"}
    rules = sorted(feed["asset_customization_rules"], key=lambda r: r["priority"])
    assert rules[0]["image_label"] == {"name": "m2_vertical"}
    assert "story" in rules[0]["customization_spec"]["instagram_positions"]
    assert rules[1]["image_label"] == {"name": "m2_feed"}
    assert c["object_story_spec"] == {"page_id": "111"}


def test_single_vertical_video_uses_video_data_with_thumbnail():
    c = m.creative_payload(
        SPEC, AD | {"key": "V1"}, {"story_video": "v1", "thumbnail_url": "https://t/x.jpg"}
    )
    vd = c["object_story_spec"]["video_data"]
    assert vd["video_id"] == "v1"
    assert vd["image_url"] == "https://t/x.jpg"
    assert vd["call_to_action"] == {"type": "SIGN_UP", "value": {"link": "https://hiredrop.io/"}}
    assert c["url_tags"] == SPEC["url_tags"]


def test_video_pair_uses_video_labels():
    c = m.creative_payload(SPEC, AD | {"key": "V2"}, {"feed_video": "a", "story_video": "b"})
    feed = c["asset_feed_spec"]
    assert feed["ad_formats"] == ["SINGLE_VIDEO"]
    assert {v["video_id"] for v in feed["videos"]} == {"a", "b"}
    assert all("video_label" in r for r in feed["asset_customization_rules"])


def test_instagram_identity_only_when_known():
    c = m.creative_payload(SPEC | {"instagram_user_id": "999"}, AD, {"feed_image": "h"})
    assert c["object_story_spec"]["instagram_user_id"] == "999"


def test_ad_is_paused():
    assert m.ad_payload(AD, "s1", "cr1") == {
        "name": "M2 · static",
        "adset_id": "s1",
        "creative": {"creative_id": "cr1"},
        "status": "PAUSED",
    }


def test_no_creative_is_an_error():
    with pytest.raises(m.MetaError):
        m.creative_payload(SPEC, AD, {})


def test_repo_spec_is_valid_and_its_enabled_creatives_exist():
    spec = m.load_spec(m.DEFAULT_SPEC) if m.DEFAULT_SPEC.exists() else None
    if spec is None:
        pytest.skip("content-lab spec lives in the workspace root, not in this repo checkout")
    assert m.campaign_payload(spec)["status"] == "PAUSED"
    for ad in m.enabled_ads(spec):
        for slot in ("feed_image", "story_image", "feed_video", "story_video"):
            if ad.get(slot):
                m.asset_path(spec, ad[slot])  # raises SystemExit when missing


def test_duplicate_ad_names_rejected(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(SPEC | {"ads": [AD, AD]}))
    with pytest.raises(SystemExit):
        m.load_spec(p)
