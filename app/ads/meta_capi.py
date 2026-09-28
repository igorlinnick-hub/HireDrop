"""Meta Conversions API — server-side conversion events for paid Meta traffic.

What gets sent, and for whom (privacy minimisation, stated in the privacy policy):
  * ONLY users Meta brought us — paid UTMs from facebook/instagram, or a Meta
    click id (fbc / fbclid) — and who did NOT opt out (`ads_optout`, GPC).
    Everyone else: nothing leaves the backend.
  * Two events, each with a deterministic event_id so a re-send is deduplicated
    by Meta instead of double-counted:
      StartTrial  act_<user_id>        first saved application (real activation)
      Purchase    pay_<stripe invoice> every invoice.paid with money collected
    (The browser pixel owns CompleteRegistration, eventID reg_<user_id>.)
  * user_data: sha256(email), sha256(user_id), and the _fbp/_fbc cookies plus
    the user agent the website stored at signup. No IP, no name, no phone.

Contract with the caller: this module NEVER blocks and NEVER raises. Every
public entry point returns immediately; the work (profile read, email lookup,
HTTP POST with a 5s timeout) runs on a daemon thread and ends in one log line.
With META_PIXEL_ID / META_CAPI_TOKEN unset it does nothing at all — no thread,
no read.
"""

import hashlib
import os
import sys
import threading
import time

import httpx

from app.ads import attribution as attr_rules

GRAPH_URL = "https://graph.facebook.com"
DEFAULT_GRAPH_VERSION = "v24.0"
EVENT_SOURCE_URL = "https://hiredrop.io/dashboard"
HTTP_TIMEOUT_SECONDS = 5.0


def _log(message: str) -> None:
    print(f"[meta_capi] {message}", file=sys.stderr)


def config() -> dict | None:
    """Env read at call time (not import time) so a Railway env change needs no
    code path restart to be honoured by tests, and unset means off."""
    pixel = os.getenv("META_PIXEL_ID", "").strip()
    token = os.getenv("META_CAPI_TOKEN", "").strip()
    if not pixel or not token:
        return None
    return {
        "pixel_id": pixel,
        "token": token,
        "test_event_code": os.getenv("META_TEST_EVENT_CODE", "").strip() or None,
        "version": os.getenv("META_GRAPH_VERSION", "").strip() or DEFAULT_GRAPH_VERSION,
    }


# --- pure pieces (unit-tested) ------------------------------------------------


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_meta_attributed(attribution: dict | None) -> bool:
    a = attribution or {}
    if attr_rules.is_meta_paid(a):
        return True
    return any(isinstance(a.get(k), str) and a[k].strip() for k in ("fbc", "fbclid"))


def should_send(attribution: dict | None) -> bool:
    """The privacy gate: Meta-attributed AND not opted out."""
    return is_meta_attributed(attribution) and not attr_rules.ads_opted_out(attribution)


def build_fbc(attribution: dict | None) -> str | None:
    """The _fbc cookie if the website stored it; else rebuilt from fbclid.

    Meta's format is fb.<subdomain_index>.<creation_time_ms>.<fbclid>; the
    creation time is when we first saw the click (captured_at).
    """
    a = attribution or {}
    fbc = a.get("fbc")
    if isinstance(fbc, str) and fbc.strip():
        return fbc.strip()
    fbclid = a.get("fbclid")
    ms = attr_rules.captured_at_ms(a)
    if isinstance(fbclid, str) and fbclid.strip() and ms:
        return f"fb.1.{ms}.{fbclid.strip()}"
    return None


def build_event(
    event_name: str,
    event_id: str,
    user_id: str,
    email: str | None,
    attribution: dict | None,
    *,
    event_time: int | None = None,
    custom_data: dict | None = None,
) -> dict:
    a = attribution or {}
    user_data: dict = {"external_id": [sha256(str(user_id))]}
    if email and email.strip():
        user_data["em"] = [sha256(email.strip().lower())]
    fbp = a.get("fbp")
    if isinstance(fbp, str) and fbp.strip():
        user_data["fbp"] = fbp.strip()
    fbc = build_fbc(a)
    if fbc:
        user_data["fbc"] = fbc
    ua = a.get("ua")
    if isinstance(ua, str) and ua.strip():
        user_data["client_user_agent"] = ua.strip()

    event = {
        "event_name": event_name,
        "event_time": int(event_time or time.time()),
        "event_id": event_id,
        "action_source": "website",
        "event_source_url": EVENT_SOURCE_URL,
        "user_data": user_data,
    }
    if custom_data:
        event["custom_data"] = custom_data
    return event


def send(events: list[dict], cfg: dict | None = None) -> bool:
    """POST events to the pixel. True on HTTP 200. Never raises."""
    cfg = cfg or config()
    if not cfg or not events:
        return False
    body: dict = {"data": events}
    if cfg["test_event_code"]:
        body["test_event_code"] = cfg["test_event_code"]
    names = ",".join(f"{e['event_name']}:{e['event_id']}" for e in events)
    try:
        # Token as a query param, as in Meta's own CAPI examples. Never logged:
        # the lines below print the event names and Meta's reply, not the URL.
        res = httpx.post(
            f"{GRAPH_URL}/{cfg['version']}/{cfg['pixel_id']}/events",
            params={"access_token": cfg["token"]},
            json=body,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"{names} not sent: {type(exc).__name__}: {exc}")
        return False
    if res.status_code != 200:
        _log(f"{names} rejected: HTTP {res.status_code} {res.text[:300]}")
        return False
    _log(f"{names} sent")
    return True


# --- reads (run on the worker thread only) ---------------------------------------


def _attribution(user_id: str) -> dict | None:
    from app.db.client import get_supabase

    res = (
        get_supabase()
        .table("profiles")
        .select("attribution")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    row = (res.data or [None])[0] or {}
    value = row.get("attribution")
    return value if isinstance(value, dict) else None


def _email_for(user_id: str) -> str | None:
    """Email lives in Supabase auth, not profiles — same lookup the night-shift
    executor uses (scripts/night_shift/executor.py)."""
    from app.db.client import get_supabase

    try:
        admin = get_supabase().auth.admin.get_user_by_id(user_id)
        return admin.user.email if admin and admin.user else None
    except Exception as exc:  # noqa: BLE001
        _log(f"email lookup failed for {str(user_id)[:8]}: {exc}")
        return None


def _is_first_application(user_id: str) -> bool:
    from app.db.client import get_supabase

    rows = (
        get_supabase()
        .table("applications")
        .select("id")
        .eq("user_id", user_id)
        .limit(2)
        .execute()
        .data
        or []
    )
    return len(rows) == 1


def _gate(user_id: str) -> dict | None:
    """The user's attribution if we may send for them, else None (logged)."""
    attribution = _attribution(user_id)
    if not should_send(attribution):
        return None
    if not (attribution or {}).get("ua"):
        # Meta requires client_user_agent on website events and rejects the
        # event without it; the website stores `ua` for exactly this gate.
        _log(f"skip {str(user_id)[:8]}: Meta-attributed but no stored user agent")
        return None
    return attribution


# --- jobs ------------------------------------------------------------------------


def _start_trial_job(user_id: str, email: str | None) -> None:
    attribution = _gate(user_id)
    if attribution is None or not _is_first_application(user_id):
        return
    event = build_event(
        "StartTrial",
        f"act_{user_id}",
        user_id,
        email or _email_for(user_id),
        attribution,
    )
    send([event])


def _purchase_job(user_id: str, invoice: dict) -> None:
    attribution = _gate(user_id)
    if attribution is None:
        return
    paid_at = invoice.get("paid_at")
    event = build_event(
        "Purchase",
        f"pay_{invoice['id']}",
        user_id,
        _email_for(user_id) or invoice.get("customer_email"),
        attribution,
        event_time=int(paid_at) if paid_at else None,
        custom_data={
            "value": round(invoice["amount_paid"] / 100, 2),
            "currency": (invoice.get("currency") or "usd").upper(),
        },
    )
    send([event])


def _run(job, *args) -> None:
    try:
        job(*args)
    except Exception as exc:  # noqa: BLE001
        _log(f"{job.__name__} failed: {type(exc).__name__}: {exc}")


def _fire(job, *args) -> None:
    threading.Thread(target=_run, args=(job, *args), daemon=True, name="meta-capi").start()


# --- public entry points (never block, never raise) --------------------------------


def track_start_trial(user_id: str, email: str | None = None) -> None:
    """Call after a real application save. Sends StartTrial if it was the first."""
    try:
        if config() and user_id:
            _fire(_start_trial_job, str(user_id), email)
    except Exception as exc:  # noqa: BLE001
        _log(f"track_start_trial not scheduled: {exc}")


def track_purchase(user_id: str, invoice: dict) -> None:
    """Call from invoice.paid. Sends Purchase for money actually collected."""
    try:
        if not config() or not user_id:
            return
        amount_paid = int(invoice.get("amount_paid") or 0)
        invoice_id = invoice.get("id")
        if amount_paid <= 0 or not invoice_id:
            return  # a $0 invoice (100% coupon, proration credit) is not a purchase
        transitions = invoice.get("status_transitions") or {}
        snapshot = {
            "id": invoice_id,
            "amount_paid": amount_paid,
            "currency": invoice.get("currency"),
            "customer_email": invoice.get("customer_email"),
            "paid_at": transitions.get("paid_at") if isinstance(transitions, dict) else None,
        }
        _fire(_purchase_job, str(user_id), snapshot)
    except Exception as exc:  # noqa: BLE001
        _log(f"track_purchase not scheduled: {exc}")
