#!/usr/bin/env python3
"""Is the fit judge right when it says no? Measured on each live user's own fresh pool.

WHY
  Today the judge (modules/ai_fit_judge.assess_fit) runs inside an AUTO walk, on the job
  PAGE, one posting at a time. The plan is to move it BEFORE the apply — judge the pool
  ahead of the walk so the deck/queue only holds jobs that pass. Before that move two
  numbers are needed per live user, and nothing else in the repo produces them:

    1. YIELD  — of the rows this user would actually be shown (the deck's own cut), how
                many clear the user's OWN bar (apply_mode: broad 35 / standard 55 /
                precise 70, ai_fit_judge._MODE_THRESHOLDS)? After the company cap
                (modules/fit_queue: 1 per company, 3 per agency, per 60 days), how many is that per day
                of pool inflow?
    2. ERRORS — when the judge says no, is it right? Only a human read of the refusals
                answers that, so the script prints them per user: every near-bar refusal
                first (bar-25 .. bar-1), topped up with random low ones, plus 5 passes.

WHAT IT CALLS — nothing re-implemented
  candidates : jobs_db.get_jobs (paged past PostgREST's 1000-row cap) -> status `new` +
               link + platform in TAP_APPLY_PLATFORMS -> on_search_filter(rows,
               get_profile(uid)) -> fresh_enough(row, DECK_MAX_AGE_DAYS). That is the
               get_deck cut (the auto ATS queue is the same rule with the 45-day cap).
               Newest first (date_found, then created_at), first --per-user rows.
  judge      : assess_fit({title, company, description}, get_profile(uid)) — exactly the
               /tools/assess-fit call, so the resume text reaches the prompt through
               resume_text_for() the same way. Cascade settings are the module's own
               (prod sets no FIT_* overrides — checked 09-30). Description is sliced to
               4000 chars like background.js ASSESS_FIT; the judge cuts it to 2500 itself.
  Two transport shims, no behavior change: the Anthropic client is wrapped to record
  `usage` (cost + budget stop + each model's raw score), and the resume download is
  memoised per URL (same bytes; one fetch per user instead of one per job).

HONESTY NOTES
  * Indeed rows in the pool usually carry only the search-card snippet; today's live
    judge reads the full page. Every row is tagged desc_kind = full (>= MIN_STORABLE_DESC,
    the backend's own "this is a posting, not a card" line) / snippet / empty, and the
    report splits by it. For snippet/empty rows the number is what a PRE-APPLY judge on
    pool data would see — not what today's page-level judge sees.
  * Applications in any status (applied / applied_unconfirmed / received) count toward
    the company rule: the employer most likely got them.
  * A user whose 14-day deck is EMPTY (a pool that stopped growing) is judged on the auto
    queue's 45-day cut instead, labelled `cut` on every row — those verdicts say whether
    the judge is right, not what the user gets today (--no-stale-fallback to skip).
  * "Gate gap": on_search_filter reads salary_min/max, salary_listed_only and work_setting,
    which app.db.profile.get_profile does not return — so in prod those gates never fire.
    The script keeps prod's behaviour and reports what the gates WOULD hide.

MEASURED 2026-09-30 (Igor broad / Antonia standard / welder precise, 411 verdicts, $2.77):
  $0.0067 per verdict, not the ~$0.003 assumed — 63% escalate, because the ±15 band sits
  where most scores land (83% for broad, bar 35). model_decision never contradicted the bar.

USAGE (from jobflow/, service_role + Anthropic keys in .env)
  .venv/bin/python scripts/measure_judge_calibration.py --dry-run          # funnel only, $0
  .venv/bin/python scripts/measure_judge_calibration.py --user <uuid> --user <uuid> \\
      --per-user 200 --budget 3.0 --out <dir>                              # judge + report
  .venv/bin/python scripts/measure_judge_calibration.py --user <uuid> --report-only --out <dir>

  Default users = top 3 by applications in the last 30 days. Re-running with the same
  --out resumes: rows already in <dir>/judged.jsonl are not paid for again, and the
  budget counts what they cost. Outputs: judged.jsonl (one row per verdict),
  summary.json, sheets.jsonl (the refusal/pass sheets printed at the end).

Read-only against Supabase (SELECTs only). Spends Anthropic money (~$0.005-0.008 per
row); stops submitting before --budget can be crossed.
"""

import argparse
import json
import os
import random
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Measurement spend is not product spend: keep it out of the ledger (modules/ai_meter.py).
os.environ.setdefault("AI_METER", "off")

import config  # noqa: E402,F401  — loads .env before the clients read the keys
from app.db import jobs as jobs_db  # noqa: E402
from app.db.client import fetch_paged, get_supabase  # noqa: E402
from app.db.profile import get_profile  # noqa: E402
from app.routers.jobs import (  # noqa: E402
    DECK_MAX_AGE_DAYS,
    MAX_POOL_AGE_DAYS,
    MIN_STORABLE_DESC,
    TAP_APPLY_PLATFORMS,
    fresh_enough,
    on_search_filter,
)
from modules import ai_cover_letter, ai_fit_judge  # noqa: E402
from modules.fit_queue import COMPANY_WINDOW_DAYS, company_cap, company_key  # noqa: E402

# $ per million tokens (input, output), Anthropic list prices — same table as
# measure_ai_cost.py. A model missing here aborts the run instead of costing $0.
PRICES = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
# Worst case for ONE assess_fit: Haiku + Sonnet, ~2.6k input tokens each, max_tokens=400.
# Used only as the safety margin of the budget stop.
MAX_JOB_USD = 0.02
# background.js ASSESS_FIT: String(q.description || "").slice(0, 4000)
EXT_DESC_SLICE = 4000

BANDS = ((0, 34), (35, 54), (55, 69), (70, 100))
NEAR_BAR = 25  # "at the bar" = score in [bar-25, bar-1]
SHEET_REFUSALS = 20
SHEET_PASSES = 5
INFLOW_DAYS = 7

_ctx = threading.local()
_lock = threading.Lock()
SPENT = {"usd": 0.0}


# ---------------------------------------------------------------- helpers


def norm_company(name: str) -> str:
    """The production cap's employer key (modules/fit_queue.company_key) — one rule, so
    this measure cuts exactly what the queue cuts."""
    return company_key(name)


def desc_kind(description: str) -> str:
    n = len((description or "").strip())
    if n == 0:
        return "empty"
    return "full" if n >= MIN_STORABLE_DESC else "snippet"


def band_of(score: int) -> str:
    for lo, hi in BANDS:
        if lo <= score <= hi:
            return f"{lo}-{hi}" if hi < 100 else f"{lo}+"
    return "?"


def _day(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _words(text: str, n: int) -> str:
    w = (text or "").split()
    return " ".join(w[:n]) + (" …" if len(w) > n else "")


def _parse_raw_score(message) -> int | None:
    """Each model's own fit_score, for cascade agreement. Telemetry only — the verdict
    is whatever assess_fit returns."""
    try:
        raw = (message.content[0].text or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        return ai_fit_judge._parse_score(json.loads(raw[start : end + 1]))
    except Exception:
        return None


# ---------------------------------------------------------------- shims


def install_shims() -> None:
    """Record usage on every judge call; memoise the resume download per URL."""
    for model in (ai_fit_judge._SCREEN_MODEL, ai_fit_judge._JUDGE_MODEL):
        if model not in PRICES:
            raise SystemExit(f"no price for {model} — add it to PRICES before spending")

    real = ai_cover_letter.get_anthropic_client()

    class Recorder:
        def create(self, **kwargs):
            message = real.messages.create(**kwargs)
            usage = message.usage
            price_in, price_out = PRICES[kwargs["model"]]
            usd = (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1e6
            calls = getattr(_ctx, "calls", None)
            if calls is not None:
                calls.append(
                    {
                        "model": kwargs["model"],
                        "in": usage.input_tokens,
                        "out": usage.output_tokens,
                        "usd": round(usd, 6),
                        "score": _parse_raw_score(message),
                    }
                )
            with _lock:
                SPENT["usd"] += usd
            return message

    client = type("RecordingClient", (), {"messages": Recorder()})()
    ai_fit_judge.get_anthropic_client = lambda: client

    original = ai_cover_letter.load_resume_text
    cache: dict = {}

    def cached_load_resume_text(resume_url=None, max_chars=3000):
        key = (resume_url, max_chars)
        with _lock:
            if key in cache:
                return cache[key]
        text = original(resume_url, max_chars=max_chars)
        with _lock:
            cache[key] = text
        return text

    # resume_text_for looks the name up in ai_cover_letter's globals at call time.
    ai_cover_letter.load_resume_text = cached_load_resume_text


def preflight_resume(uid: str, profile: dict) -> int:
    """The resume must come from THIS user's storage object. load_resume_text silently
    falls back to a local data/resume.pdf when the download fails — on a dev machine that
    is somebody else's resume, which would poison every verdict. Refuse instead."""
    url = profile.get("resume_url")
    if not url:
        raise SystemExit(f"{uid[:8]}: no resume_url — the judge would read 'Not provided'")
    get_supabase().storage.from_("resumes").download(url)  # raises if unreachable
    text = ai_cover_letter.resume_text_for(profile)
    if len(text) < 200:
        raise SystemExit(f"{uid[:8]}: resume text only {len(text)} chars — refusing to judge")
    return len(text)


# ---------------------------------------------------------------- DB reads


def top_active_users(days: int, n: int) -> list[str]:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()

    def build(start, end):
        return (
            get_supabase()
            .table("applications")
            .select("id,user_id")
            .gte("date_applied", since)
            .order("date_applied", desc=True)
            .order("id")
            .range(start, end)
        )

    rows = fetch_paged(build, 100_000)
    return [uid for uid, _ in Counter(r["user_id"] for r in rows).most_common(n)]


def recent_applications(uid: str, days: int) -> list[dict]:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()

    def build(start, end):
        return (
            get_supabase()
            .table("applications")
            .select("id,company,job_title,platform,status,date_applied,jobs(company)")
            .eq("user_id", uid)
            .gte("date_applied", since)
            .order("date_applied", desc=True)
            .order("id")
            .range(start, end)
        )

    out = []
    for r in fetch_paged(build, 20_000):
        company = (r.get("company") or "").strip() or ((r.get("jobs") or {}).get("company") or "")
        out.append({**r, "company_resolved": company})
    return out


def raw_profile_gates(uid: str) -> dict:
    """Columns on_search_filter reads but app.db.profile.get_profile does not return
    (salary_min/max, salary_listed_only, work_setting). Read only to measure the gap."""
    res = (
        get_supabase()
        .table("profiles")
        .select("salary_min,salary_max,salary_listed_only,work_setting")
        .eq("user_id", uid)
        .execute()
    )
    return res.data[0] if res.data else {}


def label_for(profile: dict) -> str:
    first = ((profile.get("name") or "user").strip().split() or ["user"])[0]
    kw = (profile.get("keywords") or ["?"])[0]
    return f"{first} ({kw}, {profile.get('apply_mode') or 'standard'})"


def build_user(uid: str, per_user: int, stale_fallback: bool = True) -> dict:
    """The deck's cut of this user's pool, plus the counts behind it."""
    profile = get_profile(uid)
    pool = jobs_db.get_jobs(uid)
    new = [j for j in pool if (j.get("status") or "new") == "new"]
    linked = [j for j in new if j.get("link") or j.get("apply_url")]
    swipeable = [j for j in linked if j.get("platform") in TAP_APPLY_PLATFORMS]
    on_search = on_search_filter(swipeable, profile)
    live = [j for j in on_search if fresh_enough(j, DECK_MAX_AGE_DAYS)]
    fresh_14d = len(live)
    cut = f"deck_{DECK_MAX_AGE_DAYS}d"
    if not live and stale_fallback:
        # The deck shows this user NOTHING today (a pool that stopped growing). To still
        # calibrate the judge on their profile, fall back to the auto ATS queue's own cap
        # (fresh_enough's default, MAX_POOL_AGE_DAYS) — then to any age. Labelled on every
        # row: these verdicts say whether the judge is right, not what the user gets today.
        live = [j for j in on_search if fresh_enough(j)]
        cut = f"queue_{MAX_POOL_AGE_DAYS}d"
        if not live:
            live, cut = list(on_search), "any_age"
    live.sort(
        key=lambda j: (str(j.get("date_found") or ""), str(j.get("created_at") or "")), reverse=True
    )
    cands = live[:per_user]

    # Not executable from the pool today (ZR by-link sits behind tapNativePool) — counted
    # so its absence is a stated exclusion, not a silent one.
    zr = [j for j in linked if j.get("platform") == "ziprecruiter"]
    zr_live = [j for j in on_search_filter(zr, profile) if fresh_enough(j, DECK_MAX_AGE_DAYS)]

    # The gate gap: what on_search_filter WOULD drop if get_profile carried these columns.
    extras = {k: v for k, v in raw_profile_gates(uid).items() if v not in (None, "", False)}
    gated_ids = None
    if extras:
        kept = {j["id"] for j in on_search_filter(cands, {**profile, **extras})}
        gated_ids = [j["id"] for j in cands if j["id"] not in kept]

    # Inflow: rows FOUND per day over the last INFLOW_DAYS full days, status-agnostic
    # (a row applied to since then was still inflow the day it landed).
    today = datetime.now(UTC).date()
    window = {today - timedelta(days=d) for d in range(1, INFLOW_DAYS + 1)}
    recent = [j for j in pool if _day(j.get("date_found")) in window]
    recent_eligible = on_search_filter(
        [
            j
            for j in recent
            if (j.get("link") or j.get("apply_url")) and j.get("platform") in TAP_APPLY_PLATFORMS
        ],
        profile,
    )
    per_day = Counter(str(_day(j.get("date_found"))) for j in recent_eligible)

    return {
        "uid": uid,
        "label": label_for(profile),
        "profile": profile,
        "cands": cands,
        "funnel": {
            "pool_rows": len(pool),
            "new": len(new),
            "new_with_link": len(linked),
            "on_tap_platforms": len(swipeable),
            "on_search": len(on_search),
            "fresh_14d": fresh_14d,
            "cut": cut,
            "sampled": len(cands),
            "platforms_sampled": dict(Counter(j.get("platform") for j in cands)),
            "desc_kinds_sampled": dict(Counter(desc_kind(j.get("description")) for j in cands)),
            "ziprecruiter_excluded_live": len(zr_live),
            "gate_gap_extras": extras,
            "gate_gap_would_hide": gated_ids,
        },
        "inflow": {
            "days": INFLOW_DAYS,
            "raw_per_day": round(len(recent) / INFLOW_DAYS, 1),
            "eligible_per_day": round(len(recent_eligible) / INFLOW_DAYS, 1),
            "eligible_by_day": dict(sorted(per_day.items())),
        },
    }


# ---------------------------------------------------------------- judging


def judge_one(user: dict, job: dict) -> dict:
    _ctx.calls = []
    description = job.get("description") or ""
    t0 = time.time()
    verdict = ai_fit_judge.assess_fit(
        job={
            "title": job.get("title") or "",
            "company": job.get("company") or "",
            "description": description[:EXT_DESC_SLICE],
        },
        profile=user["profile"],
        screener_questions=None,
    )
    calls = list(_ctx.calls)
    _ctx.calls = None
    return {
        "user_id": user["uid"],
        "user": user["label"],
        "apply_mode": verdict.get("apply_mode"),
        "threshold": verdict.get("threshold"),
        "cut": user["funnel"]["cut"],
        "job_id": job.get("id"),
        "title": job.get("title") or "",
        "company": job.get("company") or "",
        "company_norm": norm_company(job.get("company")),
        "platform": job.get("platform"),
        "date_found": job.get("date_found"),
        "location": job.get("location"),
        "job_type": job.get("job_type"),
        "link": job.get("link"),
        "pool_score": job.get("score"),
        "desc_len": len(description.strip()),
        "desc_kind": desc_kind(description),
        "desc_head": description.strip()[:400],
        "fit_score": verdict.get("fit_score"),
        "decision": verdict.get("decision"),
        "model_decision": verdict.get("model_decision"),
        "reason": verdict.get("reason"),
        "concerns": verdict.get("concerns"),
        "judged": verdict.get("judged"),
        "fail_closed": bool(verdict.get("fail_closed")),
        "judge_model": verdict.get("judge_model"),
        "escalated": verdict.get("escalated"),
        "calls": calls,
        "cost_usd": round(sum(c["usd"] for c in calls), 6),
        "secs": round(time.time() - t0, 2),
    }


def load_rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def run_judge(users: list[dict], out_path: str, budget: float, workers: int) -> None:
    prior = load_rows(out_path)
    done = {(r["user_id"], r["job_id"]) for r in prior if r.get("judged")}
    SPENT["usd"] = sum(r.get("cost_usd") or 0 for r in prior)
    print(f"\nprior rows: {len(prior)} (${SPENT['usd']:.4f} already spent) · budget ${budget:.2f}")

    # Round-robin across users, newest first within each: a budget stop leaves every
    # user with a comparable sample instead of one fully judged and one untouched.
    queues = [[(u, j) for j in u["cands"] if (u["uid"], j["id"]) not in done] for u in users]
    order = []
    for i in range(max((len(q) for q in queues), default=0)):
        order.extend(q[i] for q in queues if i < len(q))
    print(f"to judge now: {len(order)} rows · {workers} workers")

    margin = MAX_JOB_USD * (workers + 1)
    stopped = False
    in_flight = set()
    n_done = 0
    with open(out_path, "a") as out, ThreadPoolExecutor(max_workers=workers) as ex:
        it = iter(order)
        while True:
            while not stopped and len(in_flight) < workers:
                nxt = next(it, None)
                if nxt is None:
                    break
                with _lock:
                    spent = SPENT["usd"]
                if spent + margin > budget:
                    stopped = True
                    print(f"  BUDGET STOP at ${spent:.4f} (margin ${margin:.2f})")
                    break
                in_flight.add(ex.submit(judge_one, *nxt))
            if not in_flight:
                break
            finished, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
            for f in finished:
                row = f.result()
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
                n_done += 1
                if n_done % 25 == 0:
                    print(f"  {n_done}/{len(order)} judged · ${SPENT['usd']:.4f}")
    print(f"judged this run: {n_done} · total spent ${SPENT['usd']:.4f}")


# ---------------------------------------------------------------- report


def _bands(rows: list[dict]) -> dict:
    c = Counter(band_of(r["fit_score"]) for r in rows)
    labels = [f"{lo}-{hi}" if hi < 100 else f"{lo}+" for lo, hi in BANDS]
    return {label: c.get(label, 0) for label in labels}


def company_rule(passed: list[dict], applications: list[dict]) -> dict:
    """How many PASSED rows the company cap (fit_queue.company_cap) removes. The count is
    order-independent: per company, allowed = max(0, cap - already applied).

    Split by cause: `dupes` is what the sample would lose on its own (k - cap), `history`
    is the extra that the user's last-60-days applications take away."""
    applied = Counter(norm_company(a["company_resolved"]) for a in applications)
    applied.pop("", None)
    by_co = defaultdict(list)
    unknown = 0
    for r in passed:
        if r["company_norm"]:
            by_co[r["company_norm"]].append(r)
        else:
            unknown += 1
    cut_history = cut_dupes = 0
    examples = []
    for co, rows in by_co.items():
        k, h = len(rows), applied.get(co, 0)
        cap = company_cap(co)
        cut = max(0, k - max(0, cap - h))
        if not cut:
            continue
        dupes = max(0, k - cap)
        cut_dupes += dupes
        cut_history += cut - dupes
        examples.append((k, f"{co} ×{k} passed, {h} applied/60d → cut {cut}"))
    return {
        "cut": cut_history + cut_dupes,
        "cut_by_history": cut_history,
        "cut_by_sample_dupes": cut_dupes,
        "unknown_company_passes": unknown,
        "applications_60d": sum(applied.values()),
        "companies_applied_60d": len(applied),
        "examples": [text for _, text in sorted(examples, reverse=True)[:6]],
    }


def summarise(user: dict, rows: list[dict], applications: list[dict]) -> dict:
    judged = [r for r in rows if r.get("judged")]
    bar = judged[0]["threshold"] if judged else None
    passed = [r for r in judged if r["decision"] == "apply"]
    full = [r for r in judged if r["desc_kind"] == "full"]
    thin = [r for r in judged if r["desc_kind"] != "full"]
    disagree = [
        r for r in judged if r.get("model_decision") and r["model_decision"] != r["decision"]
    ]
    rule = company_rule(passed, applications)
    kept = len(passed) - rule["cut"]
    rate = kept / len(judged) if judged else 0.0
    escalated = [r for r in judged if r.get("escalated")]
    flips = 0
    for r in escalated:
        scores = {c["model"]: c["score"] for c in r.get("calls") or []}
        hs = scores.get(ai_fit_judge._SCREEN_MODEL)
        if hs is not None and (hs >= bar) != (r["fit_score"] >= bar):
            flips += 1
    cost = sum(r.get("cost_usd") or 0 for r in rows)
    gated = set(user["funnel"].get("gate_gap_would_hide") or [])
    return {
        "user": user["label"],
        "apply_mode": judged[0]["apply_mode"] if judged else None,
        "bar": bar,
        "funnel": user["funnel"],
        "judged": len(judged),
        "fail_closed": len(rows) - len(judged),
        "desc_kinds": dict(Counter(r["desc_kind"] for r in judged)),
        "bands": _bands(judged),
        "bands_full_desc": _bands(full),
        "bands_thin_desc": _bands(thin),
        "passed": len(passed),
        "passed_full_desc": sum(1 for r in full if r["decision"] == "apply"),
        "passed_thin_desc": sum(1 for r in thin if r["decision"] == "apply"),
        "pass_by_platform": {
            p: f"{sum(1 for r in judged if r['platform'] == p and r['decision'] == 'apply')}/"
            f"{sum(1 for r in judged if r['platform'] == p)}"
            for p in sorted({r["platform"] for r in judged})
        },
        "model_decision_vs_bar": {
            "disagree": len(disagree),
            "model_apply_below_bar": sum(1 for r in disagree if r["model_decision"] == "apply"),
            "model_skip_at_or_above_bar": sum(1 for r in disagree if r["model_decision"] == "skip"),
            "no_model_decision": sum(1 for r in judged if not r.get("model_decision")),
        },
        "company_rule": rule,
        "kept_after_company_rule": kept,
        "kept_rate": round(rate, 3),
        "inflow": user["inflow"],
        "fit_today_per_day": round(user["inflow"]["eligible_per_day"] * rate, 1),
        "gate_gap_hidden_in_sample": len(gated & {r["job_id"] for r in judged}),
        "gate_gap_hidden_passes": len(gated & {r["job_id"] for r in passed}),
        "cost_usd": round(cost, 4),
        "usd_per_job": round(cost / len(rows), 5) if rows else None,
        "escalated": len(escalated),
        "escalated_share": round(len(escalated) / len(judged), 3) if judged else None,
        "cascade_flips_after_escalation": flips,
        "secs_per_job": round(sum(r.get("secs") or 0 for r in rows) / len(rows), 2)
        if rows
        else None,
    }


def sheets(rows: list[dict], seed: int) -> dict:
    judged = [r for r in rows if r.get("judged")]
    if not judged:
        return {"near_bar": [], "refusals": [], "passes": []}
    bar = judged[0]["threshold"]
    refused = [r for r in judged if r["decision"] == "skip"]
    near = sorted(
        [r for r in refused if bar - NEAR_BAR <= r["fit_score"] <= bar - 1],
        key=lambda r: -r["fit_score"],
    )
    rng = random.Random(seed)  # noqa: S311 — a reproducible sample, not a secret
    low = [r for r in refused if r["fit_score"] < bar - NEAR_BAR]
    rng.shuffle(low)
    picked = (near + low)[:SHEET_REFUSALS]
    passed = [r for r in judged if r["decision"] == "apply"]
    rng.shuffle(passed)
    return {"near_bar_total": len(near), "refusals": picked, "passes": passed[:SHEET_PASSES]}


def _line(r: dict) -> str:
    has = {"full": "да", "snippet": "сниппет", "empty": "нет"}[r["desc_kind"]]
    return (
        f"{r['fit_score']:>3} | {r['title'][:60]} | {(r['company'] or '?')[:30]} | "
        f"{r['platform']} | {has} | {_words(r.get('reason') or '', 25)}"
    )


def report(users: list[dict], out_dir: str, seed: int) -> None:
    rows = load_rows(os.path.join(out_dir, "judged.jsonl"))
    summary, sheet_rows = {}, []
    for u in users:
        # Every verdict on file for this user — each row was a candidate when it was judged,
        # even if newer arrivals have since pushed it out of the newest-N slice.
        mine = [r for r in rows if r["user_id"] == u["uid"]]
        apps = recent_applications(u["uid"], COMPANY_WINDOW_DAYS)
        s = summarise(u, mine, apps)
        summary[u["uid"]] = s
        sh = sheets(mine, seed)

        print("\n" + "=" * 100)
        print(
            f"{s['user']} — bar {s['bar']} · judged {s['judged']} of {s['funnel']['sampled']} sampled"
        )
        f = s["funnel"]
        print(
            f"  funnel: pool {f['pool_rows']} → new {f['new']} → link {f['new_with_link']} → "
            f"tap-platforms {f['on_tap_platforms']} → on-search {f['on_search']} → ≤14d {f['fresh_14d']} "
            f"→ sampled {f['sampled']} (cut {f['cut']}) · ZR excluded {f['ziprecruiter_excluded_live']}"
        )
        print(f"  platforms {f['platforms_sampled']} · desc {s['desc_kinds']}")
        print(
            f"  bands all {s['bands']} · full {s['bands_full_desc']} · thin {s['bands_thin_desc']}"
        )
        print(
            f"  passed own bar: {s['passed']}/{s['judged']} (full desc {s['passed_full_desc']}, "
            f"thin {s['passed_thin_desc']}) · by platform {s['pass_by_platform']}"
        )
        print(
            f"  model decision vs bar: {s['model_decision_vs_bar']} · fail-closed {s['fail_closed']}"
        )
        cr = s["company_rule"]
        print(
            f"  company rule: cuts {cr['cut']} (history {cr['cut_by_history']}, dupes {cr['cut_by_sample_dupes']}) "
            f"· {cr['applications_60d']} apps/60d · {cr['examples']}"
        )
        inf = s["inflow"]
        print(
            f"  inflow: {inf['raw_per_day']}/day raw, {inf['eligible_per_day']}/day eligible "
            f"{inf['eligible_by_day']} → fit today ≈ {s['fit_today_per_day']}/day (kept rate {s['kept_rate']})"
        )
        if f.get("gate_gap_extras"):
            print(
                f"  gate gap (get_profile drops {sorted(f['gate_gap_extras'])}): would hide "
                f"{s['gate_gap_hidden_in_sample']} judged rows, {s['gate_gap_hidden_passes']} of them passes"
            )
        print(
            f"  cost ${s['cost_usd']} · ${s['usd_per_job']}/job · escalated {s['escalated']} "
            f"({s['escalated_share']}) · Haiku→Sonnet verdict flips {s['cascade_flips_after_escalation']} "
            f"· {s['secs_per_job']}s/job"
        )
        print(
            f"\n  REFUSALS ({sh.get('near_bar_total', 0)} near the bar, first) — score | title | company | platform | desc | reason"
        )
        for r in sh["refusals"]:
            print("   " + _line(r))
        print("  PASSES (contrast)")
        for r in sh["passes"]:
            print("   " + _line(r))
        for kind in ("refusals", "passes"):
            for r in sh[kind]:
                sheet_rows.append(
                    {
                        "user": s["user"],
                        "sheet": kind,
                        "fit_score": r["fit_score"],
                        "title": r["title"],
                        "company": r["company"],
                        "platform": r["platform"],
                        "desc_kind": r["desc_kind"],
                        "reason": r.get("reason"),
                        "concerns": r.get("concerns"),
                        "job_id": r["job_id"],
                        "line": _line(r),
                    }
                )

    total = sum(s["cost_usd"] for s in summary.values())
    n = sum(s["judged"] + s["fail_closed"] for s in summary.values())
    print(f"\nTOTAL: {n} verdicts · ${total:.4f} · ${total / max(1, n):.5f}/job")
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(out_dir, "sheets.jsonl"), "w") as f:
        for r in sheet_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {out_dir}/summary.json, sheets.jsonl")


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--user", action="append", help="user_id to measure (repeatable)")
    ap.add_argument("--top", type=int, default=3, help="default: top N by applications in 30 days")
    ap.add_argument("--per-user", type=int, default=200)
    ap.add_argument(
        "--budget", type=float, default=3.0, help="hard USD ceiling, prior rows included"
    )
    ap.add_argument(
        "--workers", type=int, default=2, help="parallel judge calls (prod key — keep low)"
    )
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "judge_calibration"))
    ap.add_argument("--seed", type=int, default=930)
    ap.add_argument(
        "--no-stale-fallback",
        action="store_true",
        help="judge nothing for a user whose 14-day deck is empty (default: fall back to the 45d queue cut)",
    )
    ap.add_argument("--dry-run", action="store_true", help="funnel only — no Anthropic calls")
    ap.add_argument("--report-only", action="store_true", help="re-summarise judged.jsonl — $0")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    uids = args.user or top_active_users(30, args.top)
    users = [build_user(uid, args.per_user, not args.no_stale_fallback) for uid in uids]
    for u in users:
        f = u["funnel"]
        print(
            f"{u['label']:<40} pool {f['pool_rows']:5d} · tap-platforms {f['on_tap_platforms']:4d} · "
            f"on-search {f['on_search']:4d} · ≤14d {f['fresh_14d']:4d} · cut {f['cut']} · sampled {f['sampled']:3d} · "
            f"{f['platforms_sampled']} · {f['desc_kinds_sampled']} · gate-gap {f['gate_gap_extras']} "
            f"hides {len(f['gate_gap_would_hide'] or [])}"
        )
    if args.dry_run:
        return 0

    if not args.report_only:
        install_shims()
        for u in users:
            u["resume_chars"] = preflight_resume(u["uid"], u["profile"])
            print(f"  resume ok for {u['label']}: {u['resume_chars']} chars")
        run_judge(users, os.path.join(args.out, "judged.jsonl"), args.budget, args.workers)
    report(users, args.out, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
