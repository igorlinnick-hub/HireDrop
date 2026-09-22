"""The affiliate program's front door: applications in, decisions out.

Five surfaces, three audiences:
  POST /affiliate/click        public  — someone opened a ?ref= link (counted, not tracked)
  POST /affiliate/apply        public  — a stranger with a QR code asks for a link
  GET  /affiliate/application  user    — "what happened to my application?"
  POST /admin/affiliates/decide admin  — approve or reject, from the admin board
  POST /admin/affiliates/issue  admin  — hand a link to someone who never applied

Nothing is automatic. A code is money, and an affiliate program that issues
codes on request is a program that gets farmed: the whole design is that a
human reads who you are before your link can earn anything. Until approval the
row lives in affiliate_applications, where the attribution trigger cannot see
it (it only matches affiliates with status='active').

Approval has two shapes, because an applicant need not have an account yet:
  account exists -> affiliates row created here, active immediately
  no account     -> the code stays reserved, and the profiles trigger in
                    migrations/add_affiliate_applications.sql materialises the
                    affiliate the moment that email signs up.

We do not email applicants (no-user-email rule): the board hands Igor the text
to send himself, and the applicant can see their own status in the dashboard.
"""

import hashlib
import hmac
import os
import re
import sys
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.db.client import get_supabase
from app.deps import get_current_user
from app.disposable_email import is_disposable_email
from config import FRONTEND_URL

router = APIRouter(tags=["affiliate"])

# Same shape the affiliates table enforces — rejected here so the applicant
# sees a useful message instead of a database error.
CODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,38}$")

# Reserved words: these read as official and would let a partner impersonate us.
RESERVED_CODES = frozenset(
    {"hiredrop", "admin", "support", "official", "team", "help", "billing", "api", "www"}
)

# In-memory sliding window, same approach as the password-reset endpoint: one
# Railway instance today. Without it this is an open write endpoint.
_WINDOW_SEC = 3600
_MAX_PER_IP = 5
# Clicks are a different shape of traffic: a lecture hall behind one NAT can
# legitimately scan the same card forty times in a minute. The ceiling is here
# to stop a script, not a crowd.
_MAX_CLICKS_PER_IP = 120
_attempts: dict[str, list[float]] = {}


def _rate_limited(key: str, ceiling: int = _MAX_PER_IP) -> bool:
    now = time.time()
    recent = [t for t in _attempts.get(key, []) if now - t < _WINDOW_SEC]
    if len(recent) >= ceiling:
        _attempts[key] = recent
        return True
    recent.append(now)
    _attempts[key] = recent
    return False


def _client_ip(request: Request) -> str:
    # Railway terminates TLS upstream, so the socket peer is the proxy.
    forwarded = request.headers.get("x-forwarded-for", "")
    return (
        forwarded.split(",")[0].strip() or (request.client.host if request.client else "")
    ) or "?"


def _require_admin_token(token: str | None) -> None:
    """Same secret as the metrics feed — the board is the only caller."""
    expected = os.getenv("ADMIN_METRICS_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Admin actions not configured")
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid admin token")


class ApplicationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    desired_code: str = Field(min_length=2, max_length=39)
    audience: str = Field(default="", max_length=2000)
    audience_size: str = Field(default="", max_length=200)
    promo_plan: str = Field(default="", max_length=2000)
    paypal_email: str = Field(default="", max_length=254)
    source: str = Field(default="", max_length=60)
    # Honeypot: a real person never fills a field they cannot see. Bots fill
    # every input they find.
    website: str = Field(default="", max_length=200)


@router.post("/affiliate/apply")
def apply(req: ApplicationRequest, request: Request) -> dict:
    """Accept an application. Public — no account required.

    Returns the same shape for "stored" and "you already applied", so this
    endpoint can't be used to check whether an email is in the program.
    """
    if req.website:
        # Silent success: telling a bot it was caught only teaches the bot.
        return {"status": "received"}

    if _rate_limited(f"ip:{_client_ip(request)}"):
        raise HTTPException(status_code=429, detail="Too many applications from here. Try later.")

    email = req.email.strip().lower()
    if "@" not in email[1:]:
        raise HTTPException(status_code=400, detail="That email doesn't look right.")
    if is_disposable_email(email):
        raise HTTPException(
            status_code=400, detail="Please apply with a permanent email — payouts depend on it."
        )

    code = req.desired_code.strip().lower()
    if not CODE_RE.match(code):
        raise HTTPException(
            status_code=400,
            detail="A link name can use lowercase letters, numbers, dot, dash and underscore.",
        )
    if code in RESERVED_CODES:
        raise HTTPException(status_code=400, detail="That link name is reserved. Pick another.")

    db = get_supabase()

    # Taken by a live affiliate? Say so now rather than at approval time, when
    # the applicant is no longer around to pick a different one.
    taken = db.table("affiliates").select("id").eq("code", code).limit(1).execute().data
    if taken:
        raise HTTPException(status_code=409, detail="That link name is taken. Pick another.")

    row = {
        "email": email,
        "name": req.name.strip(),
        "desired_code": code,
        "audience": req.audience.strip() or None,
        "audience_size": req.audience_size.strip() or None,
        "promo_plan": req.promo_plan.strip() or None,
        "paypal_email": (req.paypal_email.strip().lower() or None),
        "source": req.source.strip() or None,
        "applicant_ip": _client_ip(request),
    }
    try:
        db.table("affiliate_applications").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "affiliate_applications_live_code_idx" in text:
            raise HTTPException(
                status_code=409, detail="That link name is taken. Pick another."
            ) from exc
        if "affiliate_applications_live_email_idx" in text:
            # Already applied: same success shape, no new row.
            return {"status": "received"}
        print(f"[affiliate.apply] insert failed: {exc}", file=sys.stderr)
        raise HTTPException(status_code=500, detail="Could not save your application.") from exc

    return {"status": "received"}


class ClickRequest(BaseModel):
    code: str = Field(min_length=1, max_length=39)
    landing_page: str = Field(default="", max_length=200)
    source: str = Field(default="", max_length=60)


# Salted so the stored hash cannot be reversed by walking the IPv4 space — with
# an unsalted digest that is a few minutes of work. Falls back to the service
# key (server-only, always present) so this endpoint cannot be silently
# deployed without a salt.
def _visitor_hash(ip: str, user_agent: str) -> str:
    salt = os.getenv("AFFILIATE_CLICK_SALT") or os.getenv("SUPABASE_SERVICE_KEY", "")
    return hashlib.sha256(f"{salt}|{ip}|{user_agent}".encode()).hexdigest()


@router.post("/affiliate/click")
def click(
    req: ClickRequest,
    request: Request,
    user_agent: str | None = Header(default=None, alias="User-Agent"),
) -> dict:
    """Count one open of a ?ref= link. Public, fire-and-forget.

    Always answers the same thing. A stranger must not be able to learn which
    codes exist by watching this endpoint's replies, and the caller is a page
    that has already rendered — an error here would be noise it cannot act on.

    Print codes (card, stka, stkb) are counted exactly like partner codes: the
    whole point of separate codes on separate artefacts is comparing them.
    """
    code = req.code.strip().lower()
    if not CODE_RE.match(code) or code in RESERVED_CODES:
        return {"status": "counted"}

    ip = _client_ip(request)
    if _rate_limited(f"click:{ip}", _MAX_CLICKS_PER_IP):
        return {"status": "counted"}

    row = {
        "code": code,
        "visitor_hash": _visitor_hash(ip, user_agent or ""),
        "landing_page": req.landing_page.strip()[:200] or None,
        "source": req.source.strip()[:60] or None,
    }
    try:
        get_supabase().table("affiliate_clicks").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        # Same visitor, same code, same day — the unique index did its job.
        text = str(exc)
        if "affiliate_clicks_unique_day_idx" not in text and "23505" not in text:
            print(f"[affiliate.click] insert failed: {exc}", file=sys.stderr)

    return {"status": "counted"}


@router.get("/affiliate/application")
def my_application(user=Depends(get_current_user)) -> dict:
    """The signed-in user's own application, if any — so the dashboard can say
    'under review' instead of 'you're not in the program'."""
    email = (getattr(user, "email", "") or "").lower()
    if not email:
        return {"application": None}
    res = (
        get_supabase()
        .table("affiliate_applications")
        .select("desired_code, status, created_at, reviewed_at")
        .eq("email", email)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return {"application": res.data[0] if res.data else None}


class DecisionRequest(BaseModel):
    application_id: str
    approve: bool
    # The admin may correct the code or the rate before approving.
    code: str | None = None
    commission_pct: float | None = Field(default=None, ge=0, le=100)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/admin/affiliates/decide")
def decide(
    req: DecisionRequest,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict:
    """Approve or reject one application.

    On approval the code becomes real: either an affiliates row now (the email
    has an account) or a reservation that the signup trigger redeems later.
    The response says which, because "approved but waiting for them to sign up"
    is a different thing to tell someone than "their link is live".
    """
    _require_admin_token(x_admin_token)
    db = get_supabase()

    found = (
        db.table("affiliate_applications")
        .select("*")
        .eq("id", req.application_id)
        .limit(1)
        .execute()
        .data
    )
    if not found:
        raise HTTPException(status_code=404, detail="Application not found")
    app_row = found[0]
    if app_row["status"] != "new":
        raise HTTPException(status_code=409, detail=f"Already {app_row['status']}")

    if not req.approve:
        db.table("affiliate_applications").update(
            {"status": "rejected", "reviewed_at": _now_iso(), "note": req.note}
        ).eq("id", req.application_id).execute()
        return {"status": "rejected"}

    code = _validate_code(req.code or app_row["desired_code"])

    update = {
        "status": "approved",
        "reviewed_at": _now_iso(),
        "desired_code": code,
        "note": req.note,
    }

    user_id = _user_id_for_email(app_row["email"])
    affiliate_id = None
    if user_id:
        insert = {
            "user_id": user_id,
            "code": code,
            "paypal_email": app_row.get("paypal_email"),
            "note": f"from application {app_row['id']}",
        }
        if req.commission_pct is not None:
            insert["commission_pct"] = req.commission_pct
        try:
            created = db.table("affiliates").insert(insert).execute().data
            affiliate_id = created[0]["id"] if created else None
        except Exception as exc:  # noqa: BLE001
            # Already an affiliate (a second application, or a code issued by
            # hand earlier) — approve the application, don't duplicate the row.
            print(f"[affiliate.decide] affiliate insert skipped: {exc}", file=sys.stderr)
        if affiliate_id:
            update["affiliate_id"] = affiliate_id

    db.table("affiliate_applications").update(update).eq("id", req.application_id).execute()

    return {
        "status": "approved",
        "code": code,
        # False means the code is reserved and goes live at their signup.
        "live_now": bool(affiliate_id),
        "link": _ref_link(code),
        # Only meaningful when the code is still reserved: the one URL that
        # turns a reservation into a live link. Sent by Igor, not by us.
        "invite_url": None if affiliate_id else _invite_link(code, app_row["email"]),
    }


def _ref_link(code: str) -> str:
    return f"{FRONTEND_URL}/?ref={code}"


def _invite_link(code: str, email: str) -> str:
    """Where to send someone whose code is reserved but who has no account.

    The reservation is redeemed by EMAIL (the trigger in
    migrations/add_affiliate_applications.sql), so the address is carried in the
    URL and prefilled — signing up with a different one quietly produces an
    account with no link, which is the failure a new partner would never
    diagnose.
    """
    return f"{FRONTEND_URL}/signup?affiliate={code}&email={quote(email)}"


def _validate_code(code: str) -> str:
    """Shape, reserved words, and 'already someone else's'. Raises 4xx."""
    code = code.strip().lower()
    if not CODE_RE.match(code):
        raise HTTPException(
            status_code=400,
            detail="A link name can use lowercase letters, numbers, dot, dash and underscore.",
        )
    if code in RESERVED_CODES:
        raise HTTPException(status_code=400, detail="That link name is reserved. Pick another.")
    if get_supabase().table("affiliates").select("id").eq("code", code).limit(1).execute().data:
        raise HTTPException(status_code=409, detail="That code is already an affiliate")
    return code


class IssueRequest(BaseModel):
    """Issue a link to someone who never filled the form — the ambassador we
    approached, not the stranger who found us."""

    email: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=2, max_length=39)
    name: str = Field(default="", max_length=120)
    commission_pct: float | None = Field(default=None, ge=0, le=100)
    paypal_email: str = Field(default="", max_length=254)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/admin/affiliates/issue")
def issue(
    req: IssueRequest,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict:
    """Mint a link for a named person, account or no account.

    Same two outcomes as an approval, because it is the same mechanism — the
    only difference is that nobody applied. Deliberately not a shortcut past
    the paper trail: an approved row lands in affiliate_applications either
    way, so the board shows every live code and where it came from.

    This is the UI twin of `scripts/affiliate_admin.py issue`, which stays for
    the terminal. Both go through the same tables; neither is authoritative.
    """
    _require_admin_token(x_admin_token)

    email = req.email.strip().lower()
    if "@" not in email[1:]:
        raise HTTPException(status_code=400, detail="That email doesn't look right.")
    code = _validate_code(req.code)

    db = get_supabase()

    # An open application from this person must be decided, not bypassed: two
    # paths writing the same code is how a partner ends up with two links and
    # one of them silently dead.
    existing = (
        db.table("affiliate_applications")
        .select("id, status, desired_code")
        .eq("email", email)
        .neq("status", "rejected")
        .limit(1)
        .execute()
        .data
    )
    if existing:
        row = existing[0]
        if row["status"] == "new":
            raise HTTPException(
                status_code=409,
                detail=f"{email} already applied for '{row['desired_code']}' — approve that instead.",
            )
        raise HTTPException(
            status_code=409,
            detail=f"{email} was already approved for '{row['desired_code']}'.",
        )

    user_id = _user_id_for_email(email)
    affiliate_id = None
    if user_id:
        insert = {
            "user_id": user_id,
            "code": code,
            "paypal_email": req.paypal_email.strip().lower() or None,
            "note": req.note or "issued by admin",
        }
        if req.commission_pct is not None:
            insert["commission_pct"] = req.commission_pct
        try:
            created = db.table("affiliates").insert(insert).execute().data
            affiliate_id = created[0]["id"] if created else None
        except Exception as exc:  # noqa: BLE001
            # user_id is UNIQUE on affiliates: this account already has a link.
            print(f"[affiliate.issue] affiliate insert failed: {exc}", file=sys.stderr)
            raise HTTPException(
                status_code=409, detail=f"{email} already has an affiliate link."
            ) from exc

    application = {
        "email": email,
        "name": req.name.strip() or email.split("@")[0],
        "desired_code": code,
        "paypal_email": req.paypal_email.strip().lower() or None,
        "source": "issued-by-admin",
        "status": "approved",
        "reviewed_at": _now_iso(),
        "note": req.note,
    }
    if affiliate_id:
        application["affiliate_id"] = affiliate_id
    try:
        db.table("affiliate_applications").insert(application).execute()
    except Exception as exc:  # noqa: BLE001
        # The affiliate row is what earns money; the paper trail failing must
        # not undo it. Without an account, though, the reservation IS the row —
        # losing it means the link never goes live, so that case is fatal.
        print(f"[affiliate.issue] application row failed: {exc}", file=sys.stderr)
        if not affiliate_id:
            raise HTTPException(
                status_code=500, detail="Could not reserve that code. Try again."
            ) from exc

    return {
        "status": "issued",
        "code": code,
        "live_now": bool(affiliate_id),
        "link": _ref_link(code),
        "invite_url": None if affiliate_id else _invite_link(code, email),
    }


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


# Enough pages to cover the account list many times over. A miss here is not
# silent: approval still succeeds, the code is reserved, and the signup trigger
# redeems it — the worst case is "live_now: false" for someone who does have an
# account, which the board shows as reserved rather than live.
_USER_LOOKUP_PAGES = 20
_USER_LOOKUP_PER_PAGE = 200


def _user_id_for_email(email: str) -> str | None:
    """Find an auth user by email. auth.users is not exposed through PostgREST,
    so the admin list endpoint is the only way in — and it has no email filter,
    hence the paging."""
    try:
        for page_no in range(1, _USER_LOOKUP_PAGES + 1):
            page = get_supabase().auth.admin.list_users(
                page=page_no, per_page=_USER_LOOKUP_PER_PAGE
            )
            users = page if isinstance(page, list) else getattr(page, "users", []) or []
            if not users:
                return None
            for u in users:
                if (getattr(u, "email", "") or "").lower() == email:
                    return str(u.id)
    except Exception as exc:  # noqa: BLE001
        print(f"[affiliate.decide] user lookup failed: {exc}", file=sys.stderr)
    return None
