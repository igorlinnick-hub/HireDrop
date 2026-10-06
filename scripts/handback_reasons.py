#!/usr/bin/env python3
"""WHY are forms handed back to the user? Reason categories per platform, over a window.

`measure_handback_share.py` answers "how often" (10-06: 67 hand-backs vs 61 applications in
14 days, 52%). Nothing answered "why" in aggregate, so every filler fix was picked from a
handful of rows read by eye. This script reads every hand-back event in the window and sorts
its free-text reason into a small set of categories, derived from the reasons the extension
actually writes (content.js handBackJob call sites + the retired night shift):

    email_code         Greenhouse emailed a verification code (security-input-*). BLOCKED by
                       decision: we never read the user's mailbox for codes.
    indeed_resume_review  Indeed "Review your resume details" (structured-data-review)
                       refused Continue with nothing visibly wrong.
    continue_refused   any other step whose Continue was refused 3× (Indeed screener /
                       demographic steps, ZR) — the unfilled labels say which question.
    missing_required   submit blocked by required fields still empty (ATS) or "No answer on
                       file for" (night shift) — see the top unfilled labels.
    validation_error   submit blocked by a form validation error with no field named.
    resume_not_attached   required resume did not attach — we never send a resume-less form.
    no_submit_button   no submit button after ~40 s.
    captcha            captcha / human check. BLOCKED by decision: we never solve captchas.
    consent_terms      a terms / consent wall not accepted in time.
    sign_in            not signed in on the board.
    other              anything else (printed in full so a new category can be added).

Also printed: the top `unfilled` labels overall (the list that decides which deterministic
handler to build next), repeats (the same job handed back again and again), when each
category was LAST seen (a category that stopped after a release is a fix that worked), and
how many handed-back jobs ended up sent anyway (an application row for the same user + job
after the hand-back, or the person marked the hand-back done).

Read the unfilled labels with care: before ext #307 (10-01) Greenhouse react-selects were
read as empty after being answered, so "country / location (city) / authorized…" on GH rows
from that period are mostly false — the real block was often the emailed code. The script
flags those rows instead of re-labelling them.

    jobflow/.venv/bin/python scripts/handback_reasons.py [--days 14] [--platform greenhouse]
                                                        [--email someone@x.com] [--examples 2]

Read-only: SELECTs only. Every multi-row read goes through fetch_paged (PostgREST silently
caps a plain select at 1000 rows).
"""

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.db.client import fetch_paged, get_supabase  # noqa: E402
from modules.job_identity import job_identity, normalized_link  # noqa: E402

# Order = print order, and the order a reason is tested in (first match wins).
CATEGORIES = [
    "email_code",
    "indeed_resume_review",
    "continue_refused",
    "missing_required",
    "validation_error",
    "resume_not_attached",
    "no_submit_button",
    "captcha",
    "consent_terms",
    "sign_in",
    "other",
]

# Binding product decisions: these categories are NOT ours to fix by filling better.
BLOCKED = {
    "email_code": "we never read the user's email for codes",
    "captcha": "we never solve captchas",
}

# Ext #307 (merged 2026-10-01 15:53 HST = 10-02 01:53 UTC) stopped reading answered
# Greenhouse react-selects as empty. Greenhouse "required fields still empty" labels from
# before it are mostly false; the script splits them out instead of re-labelling.
_GH_307_AT = "2026-10-02T01:53"

# Labels that are the email-code boxes themselves, not questions: kept out of the label
# table, or six "security-input-N" rows would bury the real questions.
_CODE_LABEL = re.compile(r"^(security code|security-input-\d+)$|verification code was sent")


def _since(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _meta(row: dict) -> dict:
    md = row.get("metadata_json")
    return md if isinstance(md, dict) else {}


def user_id_for(email: str) -> str:
    for u in get_supabase().auth.admin.list_users(per_page=1000):
        if (u.email or "").lower() == email.lower():
            return u.id
    sys.exit(f"no user with email {email}")


def _reason_text(row: dict) -> str:
    """The reason as written. Night-shift lines carry it only in the message."""
    md = _meta(row)
    if md.get("reason"):
        return md["reason"]
    msg = row.get("message") or ""
    # "🌙 Night shift needs your hands [outcome=… board=…]: <title> @ <co> — <reason>"
    # "✋ Needs your hands: <title> @ <co> — <reason>. Finish it yourself: <url>"
    msg = re.sub(r"\.?\s*Finish it yourself:.*$", "", msg)
    return msg.split(" — ", 1)[1] if " — " in msg else msg


def _url(row: dict) -> str:
    md = _meta(row)
    if md.get("job_url"):
        return md["job_url"]
    m = re.search(r"Finish it yourself: (\S+)", row.get("message") or "")
    return m.group(1) if m else ""


def _labels(row: dict) -> list[str]:
    raw = _meta(row).get("unfilled") or []
    return [re.sub(r"\s+", " ", str(x)).strip().lower()[:80] for x in raw if str(x).strip()]


def categorize(row: dict) -> str:
    reason = _reason_text(row).lower()
    msg = (row.get("message") or "").lower()
    labels = _labels(row)
    path = (_meta(row).get("diag") or {}).get("path") or _url(row)
    if (
        "verification code" in reason
        or "outcome=captcha_code" in msg
        or any(lb.startswith("security-input") or lb == "security code" for lb in labels)
    ):
        return "email_code"
    if "refused" in reason and "structured-data-review" in path:
        return "indeed_resume_review"
    if "refused" in reason:
        return "continue_refused"
    if "required fields still empty" in reason or "no answer on file" in reason:
        return "missing_required"
    if "validation error" in reason:
        return "validation_error"
    if "resume didn't attach" in reason or "no resume attached" in reason:
        return "resume_not_attached"
    if "no submit button" in reason:
        return "no_submit_button"
    if "captcha" in reason or "human check" in reason or "just a moment" in reason:
        return "captcha"
    if "terms" in reason or "consent" in reason:
        return "consent_terms"
    if "signed in" in reason or "sign in" in reason:
        return "sign_in"
    # Night-shift "handback" lines whose reason is only the unfilled list.
    if labels and not reason.strip():
        return "missing_required"
    return "other"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _company_title(row: dict) -> tuple[str, str]:
    md = _meta(row)
    co, title = md.get("company") or "", md.get("job_title") or ""
    if not (co and title):
        # "... needs your hands[...]: <title> @ <company> — ..."
        m = re.search(r"hands(?: \[[^\]]*\])?: (.+?) @ (.+?) — ", row.get("message") or "")
        if m:
            title, co = title or m.group(1), co or m.group(2)
    return co, title


def job_key(row: dict) -> str:
    """One key per POSTING. Indeed is keyed by company + title, ATS boards by the posting
    id in the URL. Older Indeed hand-backs carry the form step
    (smartapply…/structured-data-review, shared by every Indeed job); since 10-06 the
    extension sends /viewjob?jk= and the table may hold a search link for old builds —
    one Indeed key for all three spellings keeps events and table rows matching."""
    url = _url(row)
    if url and "indeed.com" not in url:
        k = job_identity(url) or normalized_link(url)
        if k:
            return k
    co, title = _company_title(row)
    return f"{_norm(co)}|{_norm(title)}"


def _short(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--platform", help="only this platform (indeed, greenhouse, lever, ashby, …)")
    ap.add_argument("--email", help="only this user")
    ap.add_argument("--examples", type=int, default=2, help="raw examples per category")
    args = ap.parse_args()

    since = _since(args.days)
    uid = user_id_for(args.email) if args.email else None
    platform = (args.platform or "").lower() or None

    # The activity log is the event record: one line per hand-back. The handbacks TABLE
    # keeps one open row per URL and is overwritten on repeats — and every Indeed hand-back
    # shares a form-step URL — so it undercounts; it is read only for resolved_at.
    def build_logs(s: int, e: int):
        q = (
            get_supabase()
            .table("activity_log")
            .select("id, timestamp, user_id, phase, message, metadata_json")
            .gte("timestamp", since)
            .or_(
                "metadata_json->>type.eq.handback,message.ilike.*Needs your hands*,message.ilike.*needs your hands*"
            )
        )
        if uid:
            q = q.eq("user_id", uid)
        return q.order("id").range(s, e)

    events = fetch_paged(build_logs, 200_000)
    for ev in events:
        ev["_platform"] = (_meta(ev).get("platform") or "unknown").lower()
        ev["_cat"] = categorize(ev)
        ev["_key"] = job_key(ev)
    if platform:
        events = [ev for ev in events if ev["_platform"] == platform]

    def build_apps(s: int, e: int):
        q = (
            get_supabase()
            .table("applications")
            .select("id, user_id, platform, company, job_title, job_url, date_applied")
            .gte("date_applied", since)
        )
        if uid:
            q = q.eq("user_id", uid)
        return q.order("id").range(s, e)

    apps = fetch_paged(build_apps, 200_000)
    if platform:
        applied_n = sum(1 for a in apps if (a.get("platform") or "").lower() == platform)
    else:
        applied_n = len(apps)

    def build_hb(s: int, e: int):
        q = (
            get_supabase()
            .table("handbacks")
            .select("user_id, url, company, job_title, resolved_at, requeued_at")
            .gte("created_at", _since(args.days + 30))
        )
        if uid:
            q = q.eq("user_id", uid)
        return q.order("id").range(s, e)

    hb_rows = fetch_paged(build_hb, 200_000)
    # Keyed like the events (job_key): every Indeed row shares a form-step URL, so a URL
    # match would mark every Indeed hand-back done when one was.
    done_keys = {
        (
            r["user_id"],
            job_key(
                {
                    "metadata_json": {
                        "job_url": r.get("url"),
                        "company": r.get("company"),
                        "job_title": r.get("job_title"),
                    }
                }
            ),
        )
        for r in hb_rows
        if r.get("resolved_at")
    }

    # "Sent anyway": an application for the same user + posting AFTER the hand-back.
    app_index: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for a in apps:
        when = _ts(a["date_applied"]) if a.get("date_applied") else None
        if not when:
            continue
        keys = {f"{_norm(a.get('company'))}|{_norm(a.get('job_title'))}"}
        if a.get("job_url"):
            k = job_identity(a["job_url"]) or normalized_link(a["job_url"])
            if k:
                keys.add(k)
        for k in keys:
            app_index[(a["user_id"], k)].append(when)

    def sent_later(ev: dict) -> bool:
        t = _ts(ev["timestamp"])
        co, title = _company_title(ev)
        keys = {ev["_key"], f"{_norm(co)}|{_norm(title)}"}
        return any(w > t for k in keys for w in app_index.get((ev["user_id"], k), []))

    hb_n = len(events)
    print(
        f"window: last {args.days} days (since {since[:16]}Z)"
        + (f"   platform={platform}" if platform else "")
        + (f"   user={args.email}" if args.email else "")
    )
    if not hb_n:
        print(f"no hand-backs in this window ({applied_n} applications)")
        return 0
    users = len({ev["user_id"] for ev in events})
    jobs = len({(ev["user_id"], ev["_key"]) for ev in events})
    print(
        f"hand-backs: {hb_n} events = {jobs} distinct jobs, {users} users   "
        f"applications: {applied_n}   share: {100 * hb_n / (hb_n + applied_n):.0f}% of finished attempts"
    )
    night = sum(1 for ev in events if ev.get("phase") == "night-shift")
    if night:
        print(f"  ({night} of them from the server night shift — retired 10-02, not the extension)")

    # ── platform × category ──────────────────────────────────────────────────────
    by_pc: Counter = Counter((ev["_platform"], ev["_cat"]) for ev in events)
    plats = sorted(
        {ev["_platform"] for ev in events}, key=lambda p: -sum(by_pc[(p, c)] for c in CATEGORIES)
    )
    cats = [c for c in CATEGORIES if any(by_pc[(p, c)] for p in plats)]
    cat_total = Counter(ev["_cat"] for ev in events)
    w = max(len(c) for c in cats) + 2
    print("\nplatform × category (events):")
    print(
        " " * w
        + "".join(f"{p[:10]:>11}" for p in plats)
        + f"{'total':>8}{'jobs':>6}{'sent later':>12}  last seen"
    )
    for c in sorted(cats, key=lambda c: -cat_total[c]):
        cev = [ev for ev in events if ev["_cat"] == c]
        cjobs = {(ev["user_id"], ev["_key"]) for ev in cev}
        sent = {(ev["user_id"], ev["_key"]) for ev in cev if sent_later(ev)}
        last = max(ev["timestamp"] for ev in cev)[:10]
        tag = f"  ⛔ {BLOCKED[c]}" if c in BLOCKED else ""
        print(
            f"{c:<{w}}"
            + "".join(f"{by_pc[(p, c)] or '·':>11}" for p in plats)
            + f"{cat_total[c]:>8}{len(cjobs):>6}{len(sent):>12}  {last}{tag}"
        )
    print(
        " " * w + "".join(f"{sum(by_pc[(p, c)] for c in cats):>11}" for p in plats) + f"{hb_n:>8}"
    )

    # ── per platform: examples ───────────────────────────────────────────────────
    for p in plats:
        print(f"\n── {p} ──")
        for c in sorted(cats, key=lambda c: -by_pc[(p, c)]):
            cev = sorted(
                (ev for ev in events if ev["_platform"] == p and ev["_cat"] == c),
                key=lambda ev: ev["timestamp"],
                reverse=True,
            )
            if not cev:
                continue
            print(f"  {c}: {len(cev)}")
            if c in ("missing_required", "continue_refused", "indeed_resume_review"):
                lc = Counter(lb for ev in cev for lb in _labels(ev) if not _CODE_LABEL.search(lb))
                nolab = sum(1 for ev in cev if not _labels(ev))
                if lc:
                    print(
                        "    labels: "
                        + "; ".join(f"{_short(k, 48)} ×{v}" for k, v in lc.most_common(6))
                    )
                if nolab:
                    print(
                        f"    {nolab} with NO unfilled label (blind — only the page snapshot says why)"
                    )
                steps = Counter(
                    re.sub(r"^.*/form/", "", _url(ev)) for ev in cev if "smartapply" in _url(ev)
                )
                if steps:
                    print(
                        "    form step: " + "; ".join(f"{k} ×{v}" for k, v in steps.most_common(4))
                    )
                if p == "greenhouse" and c == "missing_required":
                    sus = sum(
                        1
                        for ev in cev
                        if ev["timestamp"] < _GH_307_AT
                        and {"country", "location (city)"} & set(_labels(ev))
                    )
                    if sus:
                        print(
                            f"    ⚠ {sus} of {len(cev)} are from before ext #307 and name country/location (city): "
                            "answered react-selects read as empty — those labels are likely FALSE"
                        )
                    post = [ev for ev in cev if ev["timestamp"] >= _GH_307_AT]
                    plc = Counter(lb for ev in post for lb in _labels(ev))
                    print(
                        f"    after #307: {len(post)} events"
                        + (
                            " — "
                            + "; ".join(f"{_short(k, 48)} ×{v}" for k, v in plc.most_common(6))
                            if plc
                            else ""
                        )
                    )
            seen = set()
            shown = 0
            for ev in cev:
                k = (_reason_text(ev)[:60], tuple(_labels(ev)[:3]))
                if k in seen:
                    continue
                seen.add(k)
                co, title = _company_title(ev)
                lab = _labels(ev)
                print(
                    f"    · {ev['timestamp'][:16]} {_short(title, 30)} @ {_short(co, 18)} — "
                    f'"{_short(_reason_text(ev), 90)}"'
                    + (f" unfilled={[_short(x, 30) for x in lab[:4]]}" if lab else "")
                )
                shown += 1
                if shown >= args.examples:
                    break

    # ── labels overall ───────────────────────────────────────────────────────────
    lc = Counter(lb for ev in events for lb in _labels(ev) if not _CODE_LABEL.search(lb))
    if lc:
        print("\ntop unfilled labels overall (events that named them; email-code boxes excluded):")
        for k, v in lc.most_common(15):
            print(f"  {v:4}  {k}")

    # ── repeats + resolution ─────────────────────────────────────────────────────
    per_job = Counter((ev["user_id"], ev["_key"]) for ev in events)
    repeats = {k: n for k, n in per_job.items() if n > 1}
    if repeats:
        extra = sum(n - 1 for n in repeats.values())
        print(
            f"\nrepeats: {len(repeats)} jobs handed back more than once = {extra} of {hb_n} events are re-runs of a known wall"
        )
        for (u, k), n in sorted(repeats.items(), key=lambda kv: -kv[1])[:5]:
            ev = next(e for e in events if e["user_id"] == u and e["_key"] == k)
            co, title = _company_title(ev)
            print(f"  ×{n}  {_short(title, 40)} @ {_short(co, 20)}  [{ev['_cat']}]")

    sent_jobs = {(ev["user_id"], ev["_key"]) for ev in events if sent_later(ev)}
    done_jobs = {
        (ev["user_id"], ev["_key"]) for ev in events if (ev["user_id"], ev["_key"]) in done_keys
    }
    print(
        f"\nresolved: {len(sent_jobs)} of {jobs} handed-back jobs were sent later (application row after the "
        f"hand-back); {len(done_jobs)} marked done by the person (handbacks.resolved_at). "
        f"The rest never reached the employer."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
