#!/usr/bin/env python3
"""Is a cheaper model as good at scoring new jobs? Every model on the SAME pool rows.

The job scorer (modules/ai_job_scorer.score_job) runs on every posting discovery brings
in, nightly for everyone who applied since the last sweep. Its 0-10 score and verdict show
on the dashboard's job list, and its ats_keywords steer resume tailoring. Moving it to a
cheaper model is safe only if both hold up, so the script lines the models up on one user's
newest pool rows (same posting, same profile, same resume — the model is the only
variable) and reports:

  * how often the scores agree (exactly, within 1, same band: 8-10 / 5-7 / 0-4) and
    whether the candidate scores higher or lower on average;
  * the band FLIPS (a strong match on one side, a skip on the other), each printed to read;
  * how much the ATS keywords overlap (Jaccard over lowercased terms);
  * $ per score from ai_meter.cost_usd, and how many calls gave no usable score.

The first model is the reference, not the truth: read the flips before switching. A
reasonable bar: same band on 9 rows in 10, flips read as borderline, keywords overlapping
at least half. Rows with and without a description are reported apart, because
without one the prompt caps the score at 5.

USAGE (from jobflow/, keys in .env)
  .venv/bin/python scripts/measure_scorer_models.py --user <uuid> --rows 80 --out scorer.md
  --models a,b  default: the scorer's model today, then claude-haiku-5-5

Spends real money: one score per row per model, well under $0.01 a row. Read-only
against Supabase. Exit 1 when a model gave no usable score at all.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config  # noqa: E402,F401  — loads .env before the clients read the keys
from modules import ai_job_scorer  # noqa: E402
from scripts.measure_letter_models import Recording, call_failure, parse_models  # noqa: E402

CANDIDATE = "claude-haiku-5-5"
SHEET_FLIPS = 15
SHEET_WIDEST = 10


def band(score: int) -> str:
    """The prompt's own score guide: 8-10 strong, 5-7 worth considering, 0-4 skip."""
    return "strong" if score >= 8 else "consider" if score >= 5 else "skip"


def jaccard(a: list[str], b: list[str]) -> float:
    left = {k.strip().lower() for k in a if k and k.strip()}
    right = {k.strip().lower() for k in b if k and k.strip()}
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def score_rows(rows: list[dict], profile: dict, resume: str, models: list[str], client) -> list:
    """Every row scored by every model, through the production score_job."""
    calls: list[dict] = []
    real = (ai_job_scorer.get_anthropic_client, ai_job_scorer.HAIKU_MODEL)
    real_default = ai_job_scorer._default_score
    ai_job_scorer.get_anthropic_client = lambda: Recording(client, calls)
    # score_job answers every failure with this neutral 5, which reads like a real score.
    # Tagged here so a fallback is counted as a failure, never as agreement.
    ai_job_scorer._default_score = lambda: {**real_default(), "fallback": True}
    out = []
    try:
        for i, row in enumerate(rows, 1):
            scores = {}
            for model in models:
                ai_job_scorer.HAIKU_MODEL = model
                mark = len(calls)
                got = ai_job_scorer.score_job(row, profile, resume)
                made = calls[mark:]
                error = call_failure(made) or (
                    "no usable score in the reply" if got.get("fallback") else None
                )
                scores[model] = {
                    "score": got["score"],
                    "reasons": got["reasons"],
                    "flags": got["flags"],
                    "ats_keywords": got["ats_keywords"],
                    "usd": sum(c.get("usd", 0.0) for c in made),
                    "in": sum(c.get("in", 0) for c in made),
                    "out": sum(c.get("out", 0) for c in made),
                    "error": error,
                }
            out.append({"job": _job_line(row), "scores": scores})
            if i % 10 == 0 or i == len(rows):
                print(f"  scored {i}/{len(rows)}")
    finally:
        ai_job_scorer.get_anthropic_client, ai_job_scorer.HAIKU_MODEL = real
        ai_job_scorer._default_score = real_default
    return out


def _job_line(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "title": row.get("title") or "",
        "company": row.get("company") or "",
        "platform": row.get("platform") or "",
        # score_job's own test: any description text at all lifts the cap of 5.
        "described": bool(row.get("description")),
    }


def per_model(scored: list[dict], model: str) -> dict:
    rows = [r["scores"][model] for r in scored]
    ok = [r for r in rows if not r["error"]]
    n = len(ok)
    return {
        "scored": n,
        "failed": len(rows) - n,
        "first_error": next((r["error"] for r in rows if r["error"]), None),
        "usd": sum(r["usd"] for r in ok) / n if n else None,
        "in": sum(r["in"] for r in ok) / n if n else None,
        "out": sum(r["out"] for r in ok) / n if n else None,
        "mean_score": sum(r["score"] for r in ok) / n if n else None,
    }


def compare(scored: list[dict], base: str, cand: str) -> dict:
    """Agreement of `cand` with `base` over the rows both scored."""
    both = [r for r in scored if not r["scores"][base]["error"] and not r["scores"][cand]["error"]]
    n = len(both)
    if not n:
        return {"rows": 0}
    deltas = [r["scores"][cand]["score"] - r["scores"][base]["score"] for r in both]
    bands = [(band(r["scores"][base]["score"]), band(r["scores"][cand]["score"])) for r in both]
    flips = [r for r, (b, c) in zip(both, bands, strict=True) if {b, c} == {"strong", "skip"}]
    overlap = [
        jaccard(r["scores"][base]["ats_keywords"], r["scores"][cand]["ats_keywords"]) for r in both
    ]
    return {
        "rows": n,
        "exact": sum(d == 0 for d in deltas) / n,
        "within_1": sum(abs(d) <= 1 for d in deltas) / n,
        "same_band": sum(b == c for b, c in bands) / n,
        "mean_abs_delta": sum(abs(d) for d in deltas) / n,
        "mean_delta": sum(deltas) / n,
        "keyword_overlap": sum(overlap) / n,
        "flips": flips,
        "widest": sorted(
            both,
            key=lambda r: abs(r["scores"][cand]["score"] - r["scores"][base]["score"]),
            reverse=True,
        ),
    }


def _pct(x: float) -> str:
    return f"{x:.0%}"


def report(scored: list[dict], models: list[str]) -> list[str]:
    lines = ["", "=" * 78, "MEASURED — every model on the same pool rows", "=" * 78]
    lines.append(
        f"  {'model':<28}{'scored':>7}{'failed':>7}{'mean':>6}{'in':>6}{'out':>5}  $/score"
    )
    for model in models:
        s = per_model(scored, model)
        if not s["scored"]:
            lines.append(
                f"  {model:<28}{0:>7}{s['failed']:>7}  no usable score: {s['first_error']}"
            )
            continue
        lines.append(
            f"  {model:<28}{s['scored']:>7}{s['failed']:>7}{s['mean_score']:>6.1f}"
            f"{s['in']:>6.0f}{s['out']:>5.0f}  ${s['usd']:.6f}"
        )
        if s["failed"]:
            lines.append(f"  {'':<28}first failure: {s['first_error']}")
    base = models[0]
    for cand in models[1:]:
        for label, subset in (
            ("all rows", scored),
            ("with a description", [r for r in scored if r["job"]["described"]]),
            ("without one", [r for r in scored if not r["job"]["described"]]),
        ):
            c = compare(subset, base, cand)
            if not c["rows"]:
                continue
            lines.append(
                f"\n  {cand} vs {base}, {label} ({c['rows']}): same band {_pct(c['same_band'])}, "
                f"within 1 {_pct(c['within_1'])}, exact {_pct(c['exact'])}, "
                f"mean {c['mean_delta']:+.2f}, keywords overlap {_pct(c['keyword_overlap'])}, "
                f"flips {len(c['flips'])}"
            )
        b, k = per_model(scored, base)["usd"], per_model(scored, cand)["usd"]
        if b and k is not None:
            lines.append(
                f"  cost: {k / b:.2f}x — ${b * 1000:.2f} vs ${k * 1000:.2f} per 1000 scores"
            )
    return lines


def _row_md(r: dict, base: str, cand: str) -> str:
    j, b, c = r["job"], r["scores"][base], r["scores"][cand]
    desc = "" if j["described"] else " (no description)"
    return (
        f"- **{b['score']} → {c['score']}** {j['company']} — {j['title']} [{j['platform']}]{desc}\n"
        f"  - {base}: {'; '.join(b['reasons'] + b['flags'])}\n"
        f"  - {cand}: {'; '.join(c['reasons'] + c['flags'])}\n"
    )


def write_markdown(scored: list[dict], models: list[str], label: str, lines: list, path: str):
    base = models[0]
    with open(path, "w") as fh:
        fh.write(f"# Job scorer: {' vs '.join(models)}\n\n{len(scored)} pool rows ({label}).\n\n")
        fh.write("```\n" + "\n".join(lines).strip() + "\n```\n")
        for cand in models[1:]:
            c = compare(scored, base, cand)
            if not c["rows"]:
                continue
            fh.write(f"\n## {cand}: band flips (strong on one side, skip on the other)\n\n")
            fh.write("".join(_row_md(r, base, cand) for r in c["flips"][:SHEET_FLIPS]) or "none\n")
            fh.write(f"\n## {cand}: widest gaps\n\n")
            fh.write("".join(_row_md(r, base, cand) for r in c["widest"][:SHEET_WIDEST]))


def load_user(uid: str, n: int) -> tuple[list[dict], dict, str, str]:
    """The newest rows of this user's pool — what the nightly sweep scored — with the
    profile and resume score_job reads for them."""
    from app.db import jobs as jobs_db
    from app.db.profile import get_profile
    from modules.ai_cover_letter import resume_text_for
    from scripts.measure_judge_calibration import label_for, preflight_resume

    profile = get_profile(uid)
    preflight_resume(uid, profile)
    rows = jobs_db.get_jobs(uid, limit=n)
    if not rows:
        raise SystemExit(f"{uid[:8]}: the pool is empty")
    return rows, profile, resume_text_for(profile), label_for(profile)


def main() -> int:
    # Measurement spend is not product spend: keep it out of the ledger (modules/ai_meter.py).
    # Set here, not on import, so a process that only imports these helpers keeps its meter.
    os.environ.setdefault("AI_METER", "off")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--user", required=True, help="user_id whose pool rows are scored")
    ap.add_argument("--rows", type=int, default=80)
    ap.add_argument(
        "--models", help=f"comma-separated, reference first (default: today's,{CANDIDATE})"
    )
    ap.add_argument("--out", default="scorer_models.md")
    args = ap.parse_args()

    models = parse_models(args.models, [ai_job_scorer.HAIKU_MODEL, CANDIDATE])
    rows, profile, resume, label = load_user(args.user, args.rows)
    print(f"{len(rows)} pool rows ({label}) x {len(models)} models: {', '.join(models)}")

    from modules.ai_cover_letter import get_anthropic_client

    scored = score_rows(rows, profile, resume, models, get_anthropic_client())
    lines = report(scored, models)
    print("\n".join(lines))
    write_markdown(scored, models, label, lines, args.out)
    spent = sum(s["usd"] for r in scored for s in r["scores"].values())
    print(f"\n  spent on this measurement: ${spent:.2f}\n  flips and widest gaps: {args.out}")
    return 1 if any(not per_model(scored, m)["scored"] for m in models) else 0


if __name__ == "__main__":
    raise SystemExit(main())
