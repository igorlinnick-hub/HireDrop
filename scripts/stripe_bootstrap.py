#!/usr/bin/env python3
"""Bring the cash register up in one command once STRIPE_SECRET_KEY is in .env.

    python scripts/stripe_bootstrap.py ship     # prices + webhook + .env + railway + verify
    python scripts/stripe_bootstrap.py status   # read-only: what exists in Stripe / prod

Idempotent: prices are found by lookup_key, the webhook endpoint by URL.
Stdlib only (same pattern as cws_publish.py) — no SDK needed for these calls.

What `ship` does:
  1. Ensure product "HireDrop Pro" + two recurring Prices ($9/week, $29/month,
     amounts imported from app/billing_config.py — single source of truth).
  2. Ensure a webhook endpoint on <prod>/api/v1/billing/webhook with the 4 events
     app/routers/billing.py handles. If the endpoint exists but we don't hold its
     signing secret (Stripe only reveals it on create), recreate it.
  3. Upsert STRIPE_PRICE_WEEKLY / STRIPE_PRICE_MONTHLY / STRIPE_WEBHOOK_SECRET
     into jobflow/.env.
  4. Push all 4 STRIPE_* vars to Railway (`railway variables --set`); if the CLI
     isn't logged in, print the exact commands to run after `railway login`.
  5. Verify prod: POST /billing/webhook with a junk body must return 400
     ("Invalid signature" = configured), not 503 ("Billing not configured").
     Polls a few minutes to ride out the Railway redeploy.
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

JOBFLOW = Path(__file__).resolve().parent.parent
ENV_PATH = JOBFLOW / ".env"
PROD_BASE = "https://web-production-db45.up.railway.app"
WEBHOOK_URL = f"{PROD_BASE}/api/v1/billing/webhook"
WEBHOOK_EVENTS = [
    "checkout.session.completed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
]
LOOKUP_KEYS = {"weekly": "hiredrop_pro_weekly", "monthly": "hiredrop_pro_monthly"}

sys.path.insert(0, str(JOBFLOW))
from app.billing_config import PLANS  # noqa: E402  (amounts: single source of truth)


def read_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def upsert_env(updates: dict) -> None:
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            seen.add(key)
    for key, val in updates.items():
        if key not in seen:
            lines.append(f"{key}={val}")
    ENV_PATH.write_text("\n".join(lines) + "\n")


def _https(url: str) -> str:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url}")
    return url


def stripe_call(secret: str, method: str, path: str, params: list[tuple] | None = None) -> dict:
    """Minimal Stripe REST client. params = list of (key, value) form pairs."""
    url = f"https://api.stripe.com{path}"
    data = None
    if method == "GET" and params:
        url += "?" + urllib.parse.urlencode(params)
    elif params is not None:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(_https(url), data=data, method=method)  # noqa: S310
    req.add_header("Authorization", f"Bearer {secret}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — scheme checked by _https
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"Stripe {method} {path} -> {e.code}:\n{body}") from None


def ensure_prices(secret: str) -> dict:
    """Return {plan_key: price_id}, creating product+prices on first run."""
    existing = stripe_call(
        secret, "GET", "/v1/prices", [("lookup_keys[]", k) for k in LOOKUP_KEYS.values()]
    )["data"]
    by_lookup = {p["lookup_key"]: p for p in existing}
    out, product_id = {}, None
    for plan_key, lookup in LOOKUP_KEYS.items():
        if lookup in by_lookup:
            out[plan_key] = by_lookup[lookup]["id"]
            print(f"  price {plan_key}: exists -> {out[plan_key]}")
            continue
        if product_id is None:
            product_id = next((p["product"] for p in by_lookup.values()), None)
        if product_id is None:
            product = stripe_call(
                secret,
                "POST",
                "/v1/products",
                [("name", "HireDrop Pro"), ("metadata[app]", "hiredrop")],
            )
            product_id = product["id"]
            print(f"  product created: {product_id}")
        plan = PLANS[plan_key]
        price = stripe_call(
            secret,
            "POST",
            "/v1/prices",
            [
                ("product", product_id),
                ("unit_amount", str(plan["price_usd"] * 100)),
                ("currency", "usd"),
                ("recurring[interval]", plan["interval"]),
                ("lookup_key", lookup),
                ("nickname", f"HireDrop Pro {plan['name']}"),
            ],
        )
        out[plan_key] = price["id"]
        print(f"  price {plan_key}: created -> {price['id']}")
    return out


def ensure_webhook(secret: str, have_signing_secret: bool) -> str | None:
    """Return the signing secret if (re)created, None if kept as-is."""
    endpoints = stripe_call(secret, "GET", "/v1/webhook_endpoints", [("limit", "100")])["data"]
    ours = [e for e in endpoints if e["url"] == WEBHOOK_URL]
    if ours and have_signing_secret:
        print(f"  webhook: exists ({ours[0]['id']}), secret already in .env — keeping")
        return None
    for e in ours:  # exists but we don't hold its secret — Stripe won't re-show it
        stripe_call(secret, "DELETE", f"/v1/webhook_endpoints/{e['id']}")
        print(f"  webhook: recreating {e['id']} (signing secret not on file)")
    ep = stripe_call(
        secret,
        "POST",
        "/v1/webhook_endpoints",
        [("url", WEBHOOK_URL)] + [("enabled_events[]", ev) for ev in WEBHOOK_EVENTS],
    )
    print(f"  webhook: created {ep['id']} -> {WEBHOOK_URL}")
    return ep["secret"]


def push_railway(updates: dict) -> bool:
    args = ["railway", "variables"]
    for k, v in updates.items():
        args += ["--set", f"{k}={v}"]
    try:
        res = subprocess.run(  # noqa: S603 — fixed executable, args built from our own keys
            args, cwd=JOBFLOW, capture_output=True, text=True, timeout=120
        )
    except FileNotFoundError:
        res = None
    if res and res.returncode == 0:
        print("  railway: 4 vars set (redeploy will follow)")
        return True
    print("  railway: CLI not available/logged in — run after `railway login`:")
    print(
        "    cd jobflow && railway variables " + " ".join(f"--set {k}=<see .env>" for k in updates)
    )
    if res:
        print(f"    ({(res.stderr or res.stdout).strip().splitlines()[-1]})")
    return False


def probe_prod() -> int:
    req = urllib.request.Request(_https(WEBHOOK_URL), data=b"{}", method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — scheme checked by _https
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def verify(minutes: float = 4) -> bool:
    """400 = signature check ran = Stripe configured in prod. 503 = not yet."""
    deadline = time.time() + minutes * 60
    while True:
        code = probe_prod()
        if code == 400:
            print("  prod webhook: 400 Invalid signature — cash register is UP")
            return True
        if time.time() > deadline:
            print(
                f"  prod webhook: still {code or 'unreachable'} after {minutes} min "
                "(503 = vars not deployed yet)"
            )
            return False
        print(f"  prod webhook: {code or 'unreachable'} — waiting for redeploy…")
        time.sleep(20)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ship"
    env = read_env()
    secret = env.get("STRIPE_SECRET_KEY", "")

    if cmd == "status":
        print(f"key in .env: {'yes (' + secret[:8] + '…)' if secret else 'NO'}")
        if secret:
            prices = stripe_call(
                secret,
                "GET",
                "/v1/prices",
                [("lookup_keys[]", k) for k in LOOKUP_KEYS.values()],
            )["data"]
            print(f"prices: {[(p['lookup_key'], p['id']) for p in prices] or 'none'}")
            eps = stripe_call(secret, "GET", "/v1/webhook_endpoints", [("limit", "100")])["data"]
            print(f"webhook: {[e['id'] for e in eps if e['url'] == WEBHOOK_URL] or 'none'}")
        print(f"prod probe: {probe_prod()} (400=configured, 503=not configured)")
        return

    if not secret:
        raise SystemExit("STRIPE_SECRET_KEY not in jobflow/.env yet — add it, then rerun.")
    if not secret.startswith("sk_"):
        raise SystemExit("STRIPE_SECRET_KEY doesn't look like a secret key (sk_...).")
    if secret.startswith("sk_test"):
        print("⚠ TEST-mode key — everything will be created in Stripe test mode.")

    print("1/4 prices")
    price_ids = ensure_prices(secret)
    print("2/4 webhook")
    signing = ensure_webhook(secret, bool(env.get("STRIPE_WEBHOOK_SECRET")))

    updates = {
        "STRIPE_PRICE_WEEKLY": price_ids["weekly"],
        "STRIPE_PRICE_MONTHLY": price_ids["monthly"],
    }
    if signing:
        updates["STRIPE_WEBHOOK_SECRET"] = signing
    upsert_env(updates)
    print(f"3/4 .env updated ({', '.join(updates)})")

    railway_vars = {**updates, "STRIPE_SECRET_KEY": secret}
    railway_vars.setdefault("STRIPE_WEBHOOK_SECRET", env.get("STRIPE_WEBHOOK_SECRET", ""))
    print("4/4 railway")
    pushed = push_railway(railway_vars)
    if pushed:
        verify()
    else:
        print("  after setting vars, check with: python scripts/stripe_bootstrap.py status")


if __name__ == "__main__":
    main()
