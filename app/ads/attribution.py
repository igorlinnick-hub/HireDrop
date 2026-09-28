"""Which channel brought a user — read from `profiles.attribution` (first touch).

Pure functions, no I/O: the admin board, the Meta CAPI gate and the tests all
classify the same way, so "paid Meta signup" means one thing everywhere.

The attribution blob is written once by the website at signup. Keys we read
(all optional): utm_source, utm_medium, utm_campaign, utm_content, utm_term,
utm_id, ref, fbclid, gclid, gbraid, wbraid, fbp, fbc, ua, ads_optout,
landing_page, captured_at.
"""

from datetime import datetime

META_SOURCES = frozenset({"facebook", "fb", "instagram", "ig", "meta"})

# utm_medium values that mean "we paid for this click". Anything else from
# facebook/google (e.g. `social`, `organic`, missing) is organic — a link in a
# post or bio is not an ad.
PAID_MEDIUMS = frozenset(
    {"paid_social", "paidsocial", "paid-social", "paid", "cpc", "ppc", "ads", "ad", "sponsored"}
)

# Google auto-tagging click ids. Unlike fbclid (which Facebook appends to
# ORGANIC outbound links too), these only exist on ad clicks.
GOOGLE_CLICK_IDS = ("gclid", "gbraid", "wbraid")

META_PAID = "meta_paid"
GOOGLE_PAID = "google_paid"
PAID_AD_CHANNELS = frozenset({META_PAID, GOOGLE_PAID})


def _norm(value) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _present(attribution: dict, key: str) -> bool:
    value = attribution.get(key)
    return isinstance(value, str) and bool(value.strip())


def source_of(attribution: dict | None) -> str:
    """The organic label: `ref:<code>`, else utm_source, else `direct`."""
    a = attribution or {}
    if isinstance(a.get("ref"), str) and a["ref"]:
        return f"ref:{a['ref']}"
    if isinstance(a.get("utm_source"), str) and a["utm_source"]:
        return a["utm_source"]
    return "direct"


def is_meta_paid(attribution: dict | None) -> bool:
    a = attribution or {}
    return _norm(a.get("utm_source")) in META_SOURCES and _norm(a.get("utm_medium")) in PAID_MEDIUMS


def is_google_paid(attribution: dict | None) -> bool:
    a = attribution or {}
    if any(_present(a, k) for k in GOOGLE_CLICK_IDS):
        return True
    return _norm(a.get("utm_source")) == "google" and _norm(a.get("utm_medium")) in PAID_MEDIUMS


def channel_of(attribution: dict | None) -> str:
    """`meta_paid` | `google_paid` | the organic source label.

    Meta is checked first because it is decided by explicit UTMs we set on
    every ad; a stray gclid on a Meta-tagged landing is not a Google click.
    fbclid alone is deliberately NOT paid: Facebook stamps it on organic
    outbound links as well.
    """
    if is_meta_paid(attribution):
        return META_PAID
    if is_google_paid(attribution):
        return GOOGLE_PAID
    return source_of(attribution)


def ads_opted_out(attribution: dict | None) -> bool:
    """The user opted out of ad-platform sharing (UI switch or GPC signal)."""
    value = (attribution or {}).get("ads_optout")
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return value is True or value == 1


def captured_at_ms(attribution: dict | None) -> int | None:
    """captured_at (ISO 8601 from the browser) as epoch milliseconds."""
    raw = (attribution or {}).get("captured_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None
