"""The affiliate program's front door: applications in, decisions out.

Three surfaces, three audiences:
  POST /affiliate/apply        public  — a stranger with a QR code asks for a link
  GET  /affiliate/application  user    — "what happened to my application?"
  POST /admin/affiliates/decide admin  — approve or reject, from the admin board

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

import hmac
import os
import re
import sys
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.db.client import get_supabase
from app.deps import get_current_user
from app.disposable_email import is_disposable_email

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
_attempts: dict[str, list[float]] = {}


def _rate_limited(key: str) -> bool:
    now = time.time()
    recent = [t for t in _attempts.get(key, []) if now - t < _WINDOW_SEC]
    if len(recent) >= _MAX_PER_IP:
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

    code = (req.code or app_row["desired_code"]).strip().lower()
    if not CODE_RE.match(code) or code in RESERVED_CODES:
        raise HTTPException(status_code=400, detail="Invalid code")
    if db.table("affiliates").select("id").eq("code", code).limit(1).execute().data:
        raise HTTPException(status_code=409, detail="That code is already an affiliate")

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
        "link": f"https://hiredrop.io/?ref={code}",
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
