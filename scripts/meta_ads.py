#!/usr/bin/env python3
"""Meta ads — build a whole campaign from one spec file, always PAUSED.

The campaign lives in a JSON spec (content-lab/ads/campaigns/meta-*.json): one
campaign, one ad set, N ads, each ad = copy + creative files. `build` uploads the
creatives and creates everything in Ads Manager with status PAUSED, then writes
the ids it created next to the spec (`<spec>.state.json`). Re-running `build`
creates only what is missing — so adding tomorrow's video is: drop the file,
set `"enabled": true` on its ad, run `build` again.

Launching is deliberately NOT here. Turning a campaign on spends money, and that
is Igor's click in Ads Manager after he has looked at the previews.

USAGE (from jobflow/, META_ADS_TOKEN in .env — system-user token with
ads_management + ads_read + pages_read_engagement + pages_manage_ads)
  .venv/bin/python scripts/meta_ads.py whoami
  .venv/bin/python scripts/meta_ads.py plan   [--spec PATH]   # print payloads, no API calls
  .venv/bin/python scripts/meta_ads.py build  [--spec PATH]   # create what's missing, PAUSED
  .venv/bin/python scripts/meta_ads.py status [--spec PATH]   # delivery + review state per ad
  .venv/bin/python scripts/meta_ads.py teardown [--spec PATH] --yes   # delete what build created

Same token feeds the spend sync (META_ADS_TOKEN in app/ads/meta_spend.py).
"""

import argparse
import json
import mimetypes
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402,F401 — importing it loads .env
from app.ads.meta_capi import DEFAULT_GRAPH_VERSION, GRAPH_URL  # noqa: E402

DEFAULT_SPEC = Path(__file__).resolve().parents[2] / "content-lab/ads/campaigns/meta-r1.json"
HTTP_TIMEOUT_SECONDS = 120.0
VIDEO_READY_TIMEOUT_SECONDS = 600

# Placements that show a 9:16 asset. Everything else (feeds, Explore, search, right
# column, Audience Network) gets the 4:5 one. Meta matches rules by priority.
VERTICAL_POSITIONS = {
    "publisher_platforms": ["facebook", "instagram", "messenger"],
    "facebook_positions": ["story", "facebook_reels"],
    "instagram_positions": ["story", "reels"],
    "messenger_positions": ["story"],
}
OTHER_POSITIONS = {
    "publisher_platforms": ["facebook", "instagram", "messenger", "audience_network"],
}


class MetaError(RuntimeError):
    pass


# --- spec ------------------------------------------------------------------------


def load_spec(path: Path) -> dict:
    spec = json.loads(path.read_text())
    spec["_dir"] = str(path.parent)
    for key in ("account_id", "pixel_id", "link", "campaign", "adset", "ads"):
        if not spec.get(key):
            raise SystemExit(f"spec {path}: missing `{key}`")
    spec["account_id"] = str(spec["account_id"]).removeprefix("act_")
    names = [a["name"] for a in spec["ads"]]
    if len(names) != len(set(names)):
        raise SystemExit(f"spec {path}: ad names must be unique (they are the idempotency key)")
    return spec


def asset_path(spec: dict, rel: str) -> Path:
    p = (Path(spec["_dir"]) / rel).resolve()
    if not p.is_file():
        raise SystemExit(f"creative file not found: {p}")
    return p


def enabled_ads(spec: dict) -> list[dict]:
    return [a for a in spec["ads"] if a.get("enabled", True)]


def state_path(spec_path: Path) -> Path:
    return spec_path.with_suffix(".state.json")


def load_state(spec_path: Path) -> dict:
    p = state_path(spec_path)
    return json.loads(p.read_text()) if p.exists() else {"images": {}, "videos": {}, "ads": {}}


def save_state(spec_path: Path, state: dict) -> None:
    state_path(spec_path).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# --- payloads (pure: unit-tested, printed by `plan`) ---------------------------------


def campaign_payload(spec: dict) -> dict:
    c = spec["campaign"]
    return {
        "name": c["name"],
        "objective": c.get("objective", "OUTCOME_LEADS"),
        "status": "PAUSED",
        # Employment: Meta rejects job-related ads without it and restricts accounts
        # that repeat. It also fixes age 18-65 and forbids gender / ZIP targeting.
        "special_ad_categories": c.get("special_ad_categories", ["EMPLOYMENT"]),
        "special_ad_category_country": c.get("special_ad_category_country", ["US"]),
        # Campaign budget (one ad set anyway); Graph wants cents.
        "daily_budget": round(float(c["daily_budget_usd"]) * 100),
        "bid_strategy": c.get("bid_strategy", "LOWEST_COST_WITHOUT_CAP"),
    }


def adset_payload(spec: dict, campaign_id: str) -> dict:
    a = spec["adset"]
    targeting: dict = {"geo_locations": {"countries": a.get("countries", ["US"])}}
    if a.get("advantage_audience", True):
        targeting["targeting_automation"] = {"advantage_audience": 1}
    # No publisher_platforms = Advantage+ placements.
    return {
        "name": a["name"],
        "campaign_id": campaign_id,
        "status": "PAUSED",
        "optimization_goal": "OFFSITE_CONVERSIONS",
        "billing_event": "IMPRESSIONS",
        "destination_type": "WEBSITE",
        "promoted_object": {
            "pixel_id": str(spec["pixel_id"]),
            "custom_event_type": a.get("event", "COMPLETE_REGISTRATION"),
        },
        "targeting": targeting,
    }


def _identity(spec: dict) -> dict:
    ident = {"page_id": str(spec["page_id"])}
    if spec.get("instagram_user_id"):
        ident["instagram_user_id"] = str(spec["instagram_user_id"])
    return ident


def creative_payload(spec: dict, ad: dict, media: dict) -> dict:
    """`media` = {"feed_image": hash, "story_image": hash, "feed_video": id,
    "story_video": id, "thumbnail_url": url} — whichever the ad uses."""
    link = ad.get("link", spec["link"])
    cta = ad.get("cta", "SIGN_UP")
    label = ad["key"].lower()
    base = {"name": f"{ad['name']} · creative", "url_tags": spec["url_tags"]}

    is_video = bool(media.get("story_video") or media.get("feed_video"))
    pair = (
        (media.get("feed_video"), media.get("story_video"))
        if is_video
        else (media.get("feed_image"), media.get("story_image"))
    )
    feed, story = pair

    if feed and story:
        # Placement asset customization: 4:5 in feeds, 9:16 in Stories/Reels.
        kind = "videos" if is_video else "images"
        key = "video_id" if is_video else "hash"
        assets = [
            {key: feed, "adlabels": [{"name": f"{label}_feed"}]},
            {key: story, "adlabels": [{"name": f"{label}_vertical"}]},
        ]
        if is_video and media.get("thumbnail_url"):
            for asset in assets:
                asset["thumbnail_url"] = media["thumbnail_url"]
        label_key = "video_label" if is_video else "image_label"
        return base | {
            "object_story_spec": _identity(spec),
            "asset_feed_spec": {
                kind: assets,
                "bodies": [{"text": ad["primary"]}],
                "titles": [{"text": ad["headline"]}],
                "link_urls": [{"website_url": link}],
                "call_to_action_types": [cta],
                "ad_formats": ["SINGLE_VIDEO" if is_video else "SINGLE_IMAGE"],
                "optimization_type": "PLACEMENT",
                "asset_customization_rules": [
                    {
                        "customization_spec": VERTICAL_POSITIONS,
                        label_key: {"name": f"{label}_vertical"},
                        "priority": 1,
                    },
                    {
                        "customization_spec": OTHER_POSITIONS,
                        label_key: {"name": f"{label}_feed"},
                        "priority": 2,
                    },
                ],
            },
        }

    only = feed or story
    if not only:
        raise MetaError(f"ad {ad['name']}: no creative uploaded")
    cta_obj = {"type": cta, "value": {"link": link}}
    if is_video:
        video_data = {
            "video_id": only,
            "message": ad["primary"],
            "title": ad["headline"],
            "call_to_action": cta_obj,
        }
        if media.get("thumbnail_url"):
            video_data["image_url"] = media["thumbnail_url"]
        story_spec = _identity(spec) | {"video_data": video_data}
    else:
        story_spec = _identity(spec) | {
            "link_data": {
                "image_hash": only,
                "link": link,
                "message": ad["primary"],
                "name": ad["headline"],
                "call_to_action": cta_obj,
            }
        }
    return base | {"object_story_spec": story_spec}


def ad_payload(ad: dict, adset_id: str, creative_id: str) -> dict:
    return {
        "name": ad["name"],
        "adset_id": adset_id,
        "creative": {"creative_id": creative_id},
        "status": "PAUSED",
    }


# --- Graph -----------------------------------------------------------------------


class Graph:
    def __init__(self, token: str, version: str):
        self.token = token
        self.base = f"{GRAPH_URL}/{version}"
        self.http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)

    def _check(self, r: httpx.Response) -> dict:
        try:
            body = r.json()
        except ValueError:
            raise MetaError(f"HTTP {r.status_code}: {r.text[:300]}") from None
        if r.status_code >= 400 or "error" in body:
            err = body.get("error", {})
            detail = err.get("error_user_msg") or err.get("message") or str(body)[:300]
            raise MetaError(f"{err.get('type', 'Error')} {err.get('code', r.status_code)}: {detail}")
        return body

    def get(self, path: str, **params) -> dict:
        params["access_token"] = self.token
        return self._check(self.http.get(f"{self.base}/{path}", params=params))

    def post(self, path: str, payload: dict, files: dict | None = None) -> dict:
        # Graph takes nested objects as JSON strings in form fields.
        data = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in payload.items()}
        data["access_token"] = self.token
        return self._check(self.http.post(f"{self.base}/{path}", data=data, files=files))

    def delete(self, object_id: str) -> dict:
        return self._check(self.http.delete(f"{self.base}/{object_id}", params={"access_token": self.token}))


def graph() -> Graph:
    token = os.getenv("META_ADS_TOKEN", "").strip()
    if not token:
        raise SystemExit("META_ADS_TOKEN is not set (system-user token, see ADS_PLAN §2 step 7)")
    return Graph(token, os.getenv("META_GRAPH_VERSION", "").strip() or DEFAULT_GRAPH_VERSION)


def upload_image(g: Graph, account: str, path: Path) -> str:
    with path.open("rb") as fh:
        body = g.post(f"act_{account}/adimages", {}, files={"filename": (path.name, fh, "image/png")})
    images = body.get("images") or {}
    if not images:
        raise MetaError(f"adimages: no hash for {path.name}: {body}")
    return next(iter(images.values()))["hash"]


def upload_video(g: Graph, account: str, path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    with path.open("rb") as fh:
        body = g.post(
            f"act_{account}/advideos", {"name": path.name}, files={"source": (path.name, fh, mime)}
        )
    video_id = body["id"]
    deadline = time.monotonic() + VIDEO_READY_TIMEOUT_SECONDS
    while True:
        status = (g.get(video_id, fields="status").get("status") or {}).get("video_status")
        if status == "ready":
            return video_id
        if status == "error":
            raise MetaError(f"video {path.name} ({video_id}) failed processing")
        if time.monotonic() > deadline:
            raise MetaError(f"video {path.name} ({video_id}) not ready after {VIDEO_READY_TIMEOUT_SECONDS}s")
        time.sleep(5)


def video_thumbnail(g: Graph, video_id: str) -> str | None:
    thumbs = g.get(f"{video_id}/thumbnails").get("data") or []
    preferred = [t for t in thumbs if t.get("is_preferred")] or thumbs
    return preferred[0]["uri"] if preferred else None


# --- commands --------------------------------------------------------------------


def cmd_whoami(args) -> int:
    g = graph()
    spec = load_spec(Path(args.spec))
    me = g.get("me", fields="id,name")
    print(f"token user : {me.get('name')} ({me.get('id')})")
    acct = g.get(
        f"act_{spec['account_id']}",
        fields="name,account_status,currency,timezone_name,disable_reason,funding_source_details",
    )
    status = {1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_RISK_REVIEW", 9: "IN_GRACE_PERIOD"}
    print(
        f"ad account : {acct.get('name')} act_{spec['account_id']} "
        f"{status.get(acct.get('account_status'), acct.get('account_status'))} "
        f"{acct.get('currency')} {acct.get('timezone_name')} "
        f"funding={'yes' if acct.get('funding_source_details') else 'NO'}"
    )
    pixel = g.get(str(spec["pixel_id"]), fields="name,last_fired_time")
    print(f"pixel      : {pixel.get('name')} {spec['pixel_id']} last fired {pixel.get('last_fired_time')}")
    pages = g.get("me/accounts", fields="id,name,instagram_business_account{id,username}").get("data") or []
    if spec.get("business_id"):
        owned = g.get(f"{spec['business_id']}/owned_pages", fields="id,name").get("data") or []
        known = {p["id"] for p in pages}
        pages += [p for p in owned if p["id"] not in known]
    for p in pages:
        ig = p.get("instagram_business_account") or {}
        print(f"page       : {p['name']} id={p['id']} instagram={ig.get('username')} ig_id={ig.get('id')}")
    if not pages:
        print("page       : NONE visible to this token — give the system user the HireDrop page")
    print(f"spec page_id={spec.get('page_id')} instagram_user_id={spec.get('instagram_user_id')}")
    return 0


def cmd_plan(args) -> int:
    spec = load_spec(Path(args.spec))
    print("campaign:", json.dumps(campaign_payload(spec), indent=2))
    print("adset   :", json.dumps(adset_payload(spec, "<campaign_id>"), indent=2))
    for ad in spec["ads"]:
        on = ad.get("enabled", True)
        files = {k: ad[k] for k in ("feed_image", "story_image", "feed_video", "story_video") if ad.get(k)}
        missing = [v for v in files.values() if not (Path(spec["_dir"]) / v).is_file()]
        print(f"\nad {ad['name']} — {'ENABLED' if on else 'disabled (skipped by build)'}")
        print(f"  files   : {files}{'  MISSING: ' + ', '.join(missing) if missing else ''}")
        media = {k: f"<{k}>" for k in files}
        if spec.get("page_id"):
            print("  creative:", json.dumps(creative_payload(spec, ad, media), indent=2))
    if not spec.get("page_id"):
        print("\n(page_id not set in spec — `whoami` lists the page id)")
    return 0


def cmd_build(args) -> int:
    spec_path = Path(args.spec)
    spec = load_spec(spec_path)
    if not spec.get("page_id"):
        raise SystemExit("spec has no page_id — run `whoami` and put the HireDrop page id into the spec")
    g = graph()
    acct = spec["account_id"]
    state = load_state(spec_path)

    def remember():
        save_state(spec_path, state)

    if not state.get("campaign_id"):
        state["campaign_id"] = g.post(f"act_{acct}/campaigns", campaign_payload(spec))["id"]
        remember()
        print(f"campaign created  {state['campaign_id']}")
    if not state.get("adset_id"):
        state["adset_id"] = g.post(f"act_{acct}/adsets", adset_payload(spec, state["campaign_id"]))["id"]
        remember()
        print(f"ad set created    {state['adset_id']}")

    for ad in enabled_ads(spec):
        if ad["name"] in state["ads"]:
            print(f"ad exists         {ad['name']} ({state['ads'][ad['name']]['ad_id']})")
            continue
        media: dict = {}
        for slot in ("feed_image", "story_image"):
            if ad.get(slot):
                rel = ad[slot]
                if rel not in state["images"]:
                    state["images"][rel] = upload_image(g, acct, asset_path(spec, rel))
                    remember()
                media[slot] = state["images"][rel]
        for slot in ("feed_video", "story_video"):
            if ad.get(slot):
                rel = ad[slot]
                if rel not in state["videos"]:
                    vid = upload_video(g, acct, asset_path(spec, rel))
                    state["videos"][rel] = {"id": vid, "thumbnail_url": video_thumbnail(g, vid)}
                    remember()
                media[slot] = state["videos"][rel]["id"]
                media.setdefault("thumbnail_url", state["videos"][rel]["thumbnail_url"])
        creative_id = g.post(f"act_{acct}/adcreatives", creative_payload(spec, ad, media))["id"]
        ad_id = g.post(f"act_{acct}/ads", ad_payload(ad, state["adset_id"], creative_id))["id"]
        state["ads"][ad["name"]] = {"ad_id": ad_id, "creative_id": creative_id}
        remember()
        print(f"ad created        {ad['name']} ({ad_id})")

    print(f"\nAll PAUSED. State: {state_path(spec_path)}")
    print(f"Review: https://adsmanager.facebook.com/adsmanager/manage/ads?act={acct}")
    return 0


def cmd_status(args) -> int:
    spec_path = Path(args.spec)
    spec = load_spec(spec_path)
    state = load_state(spec_path)
    if not state.get("campaign_id"):
        print("nothing built yet")
        return 0
    g = graph()
    for label, oid in (("campaign", state["campaign_id"]), ("ad set", state.get("adset_id"))):
        if oid:
            o = g.get(oid, fields="name,status,effective_status")
            print(f"{label:9} {o.get('effective_status'):<16} {o.get('name')}")
    for name, ids in state["ads"].items():
        o = g.get(ids["ad_id"], fields="name,effective_status,ad_review_feedback")
        feedback = o.get("ad_review_feedback") or {}
        print(f"ad        {o.get('effective_status'):<16} {name}")
        for kind, reasons in feedback.items():
            for reason, text in (reasons or {}).items():
                print(f"          {kind}: {reason} — {text}")
    _ = spec
    return 0


def cmd_teardown(args) -> int:
    if not args.yes:
        raise SystemExit("teardown deletes the campaign build created — pass --yes")
    spec_path = Path(args.spec)
    state = load_state(spec_path)
    if not state.get("campaign_id"):
        print("nothing to delete")
        return 0
    g = graph()
    g.delete(state["campaign_id"])  # cascades to the ad set and ads
    print(f"deleted campaign {state['campaign_id']} (ad set + ads with it)")
    state_path(spec_path).unlink()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("whoami", "plan", "build", "status", "teardown"):
        p = sub.add_parser(name)
        p.add_argument("--spec", default=str(DEFAULT_SPEC))
        if name == "teardown":
            p.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    try:
        return {
            "whoami": cmd_whoami,
            "plan": cmd_plan,
            "build": cmd_build,
            "status": cmd_status,
            "teardown": cmd_teardown,
        }[args.cmd](args)
    except MetaError as exc:
        print(f"Meta API: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
