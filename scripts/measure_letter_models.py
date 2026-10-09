#!/usr/bin/env python3
"""Put cover-letter models side by side on the SAME postings — cost and text.

Judging a model swap needs every model's letter for the same posting, same prompt, same
resume, so the model is the only variable. The letters go through the production
generate_cover_letter with only the model constant changed; the price comes from
ai_meter.cost_usd, the same function the spend ledger uses.

Read the letters, not only the numbers. A preamble like "Here's a cover letter for
Jordan:" pasted into an employer's form is invisible to every cost measurement, and
whether a newer model writes better is a call a human makes by reading.

USAGE (from jobflow/, keys in .env)
  # A real user's letters: their profile and resume, postings they applied to lately
  .venv/bin/python scripts/measure_letter_models.py --user <uuid> --jobs 8 --out letters.md
  # A fixed sample with a synthetic resume, no Supabase
  #   sample.json: [{"title": …, "company": …, "description": …}, …]
  .venv/bin/python scripts/measure_letter_models.py --sample sample.json --out letters.md

  --models a,b[,c]  default: the model letters ship with today, then claude-sonnet-5-5.
                    The first one is the baseline the others are compared with.

Spends real money: one letter per posting per model, about $0.01 a letter. Read-only
against Supabase. Exit 1 when a model wrote no letter at all (an API error is printed).
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config  # noqa: E402,F401  — loads .env before the clients read the keys
from modules import ai_cover_letter, ai_meter  # noqa: E402
from modules.ai_models import plain_answer_kwargs, refused  # noqa: E402
from modules.ai_spend import CEILING_PER_APPLICATION_USD  # noqa: E402

CANDIDATE = "claude-sonnet-5-5"
APPLICATIONS_PER_MONTH_AT_CAP = 30 * 30

# Same length resume_text_for hands the prompt.
SYNTHETIC_RESUME = (
    (
        "JORDAN AVERY — Project Manager\njordan.avery@example.com | Austin, TX\n\n"
        "EXPERIENCE\nSenior Project Manager, Northwind Logistics (2021-2026)\n"
        "Ran cross-functional delivery for a 40-person org; shipped a warehouse "
        "routing rebuild that cut per-order handling time by a fifth. Owned vendor "
        "negotiation, quarterly roadmap and the incident review process.\n"
        "Project Manager, Cedar Systems (2018-2021)\nCoordinated three engineering "
        "teams through a platform migration. Built the intake process the company "
        "still uses.\n\nSKILLS\nJira, SQL, stakeholder management, agile delivery, "
        "vendor management, risk registers, budget ownership\n\nEDUCATION\n"
        "BS Industrial Engineering, University of Texas\n\n"
    )
    * 3
)[:3000]

SYNTHETIC_PROFILE = {
    "name": "Jordan",
    "last_name": "Avery",
    "email": "jordan.avery@example.com",
    "apply_mode": "standard",
    "resume_url": None,
    "keywords": ["project manager"],
}


def parse_models(spec: str | None, default: list[str] | None = None) -> list[str]:
    """The models to compare, baseline first. Refuses, before any money is spent, a model the
    ledger cannot price (every number below is a real price) or cannot be asked for a plain
    answer (it would spend the answer's tokens thinking)."""
    named = [m.strip() for m in (spec or "").split(",") if m.strip()]
    models = list(
        dict.fromkeys(named or default or [ai_cover_letter.COVER_LETTER_MODEL, CANDIDATE])
    )
    if len(models) < 2:
        raise SystemExit("Name at least two different models: --models a,b")
    unpriced = [m for m in models if ai_meter.price_of(m) is None]
    if unpriced:
        raise SystemExit(f"No price in ai_meter.PRICES for {unpriced}: add it first.")
    for model in models:
        try:
            plain_answer_kwargs(model)
        except ValueError as e:
            raise SystemExit(str(e)) from e
    return models


class Recording:
    """The real client, with every call's tokens, price, refusal, cut-off or error written
    down."""

    def __init__(self, client, calls: list[dict]):
        self._client = client
        self._calls = calls
        self.messages = self

    def create(self, **kwargs):
        model = kwargs["model"]
        try:
            message = self._client.messages.create(**kwargs)
        except Exception as e:
            self._calls.append({"model": model, "error": f"{type(e).__name__}: {e}"})
            raise
        usage = message.usage
        self._calls.append(
            {
                "model": model,
                "in": usage.input_tokens,
                "out": usage.output_tokens,
                "usd": float(ai_meter.cost_usd(model, usage) or 0),
                "refused": refused(message),
                "cut": getattr(message, "stop_reason", None) == "max_tokens",
            }
        )
        return message


def _outcome(letter: dict) -> str:
    return "FAILED" if letter["error"] else f"${letter['usd']:.5f}"


def call_failure(made: list[dict]) -> str | None:
    """Why the calls behind one answer gave no usable answer, or None. The product answers
    each of these with a stand-in (a template letter, a neutral score), so the recorded
    calls are the only place the failure shows."""
    if not made:
        return "no API call (is ANTHROPIC_API_KEY set?)"
    error = next((c["error"] for c in made if "error" in c), None)
    if error:
        return error
    if any(c.get("refused") for c in made):
        return "refused"
    if any(c.get("cut") for c in made):
        return "cut off at max_tokens"
    return None


def write_letters(
    jobs: list[dict], profile: dict, resume: str, models: list[str], client
) -> list[dict]:
    """One letter per posting per model, through the production generate_cover_letter."""
    calls: list[dict] = []
    real_getter = ai_cover_letter.get_anthropic_client
    real_resume = ai_cover_letter.resume_text_for
    shipped = ai_cover_letter.COVER_LETTER_MODEL
    ai_cover_letter.get_anthropic_client = lambda: Recording(client, calls)
    # Read once for the whole run: every letter is written from the same resume.
    ai_cover_letter.resume_text_for = lambda *_a, **_k: resume
    pairs = []
    try:
        for i, job in enumerate(jobs, 1):
            letters = {}
            for model in models:
                mark = len(calls)
                ai_cover_letter.COVER_LETTER_MODEL = model
                text = ai_cover_letter.generate_cover_letter(job, profile)
                made = calls[mark:]
                letters[model] = {
                    "text": text,
                    "chars": len(text),
                    "in": sum(c.get("in", 0) for c in made),
                    "out": sum(c.get("out", 0) for c in made),
                    "usd": sum(c.get("usd", 0.0) for c in made),
                    "error": call_failure(made),
                }
            pairs.append({"job": job, "letters": letters})
            line = "   ".join(f"{m}: {_outcome(d)}" for m, d in letters.items())
            print(f"  {i:2}. {(job.get('company') or '?')[:20]:<20} {line}")
    finally:
        ai_cover_letter.COVER_LETTER_MODEL = shipped
        ai_cover_letter.get_anthropic_client = real_getter
        ai_cover_letter.resume_text_for = real_resume
    return pairs


def summarize(pairs: list[dict], models: list[str]) -> dict:
    """Per model: averages over the letters it actually wrote, plus what it failed on.
    The other models are compared with the first one."""
    out: dict = {}
    for model in models:
        rows = [p["letters"][model] for p in pairs]
        written = [r for r in rows if not r["error"]]
        n = len(written)
        out[model] = {
            "letters": n,
            "errors": len(rows) - n,
            "usd": sum(r["usd"] for r in written) / n if n else None,
            "in": sum(r["in"] for r in written) / n if n else None,
            "out": sum(r["out"] for r in written) / n if n else None,
            "chars": sum(r["chars"] for r in written) / n if n else None,
            "first_error": next((r["error"] for r in rows if r["error"]), None),
        }
    base = out[models[0]]["usd"]
    for model in models[1:]:
        usd = out[model]["usd"]
        if base and usd is not None:
            delta = usd - base
            out[model]["vs_baseline"] = {
                "delta_usd": delta,
                "ratio": usd / base,
                "per_month_at_cap": delta * APPLICATIONS_PER_MONTH_AT_CAP,
            }
    return out


def print_summary(summary: dict, models: list[str]) -> None:
    print("\n" + "=" * 78)
    print("MEASURED — real usage, every model on the same postings")
    print("=" * 78)
    print(f"  {'model':<26}{'letters':>8}{'in':>7}{'out':>6}{'chars':>7}{'$/letter':>11}")
    for model in models:
        s = summary[model]
        if not s["letters"]:
            print(f"  {model:<26}{0:>8}   no letter: {s['first_error']}")
            continue
        print(
            f"  {model:<26}{s['letters']:>8}{s['in']:>7.0f}{s['out']:>6.0f}{s['chars']:>7.0f}"
            f"  ${s['usd']:.5f}"
        )
        if s["errors"]:
            print(f"  {'':<26}{s['errors']} failed, first: {s['first_error']}")
    for model in models[1:]:
        cmp = summary[model].get("vs_baseline")
        if cmp:
            print(
                f"\n  {model} vs {models[0]}: {cmp['delta_usd']:+.5f} $/letter "
                f"({cmp['ratio']:.2f}x), {cmp['per_month_at_cap']:+.2f} $/month at the 30/day cap"
            )
    for model in models:
        usd = summary[model]["usd"]
        if usd is not None:
            print(
                f"  a {model} letter is {usd / CEILING_PER_APPLICATION_USD:.0%} of the "
                f"${CEILING_PER_APPLICATION_USD} per-application ceiling"
            )


def write_markdown(
    pairs: list[dict], summary: dict, models: list[str], source: str, path: str
) -> None:
    with open(path, "w") as fh:
        fh.write(f"# Cover letters: {' vs '.join(models)}\n\n")
        fh.write(
            f"{len(pairs)} postings ({source}), one letter from each model: same prompt, same "
            "resume, the model is the only variable.\n\n"
        )
        for model in models:
            s = summary[model]
            price = f"${s['usd']:.5f}/letter" if s["usd"] is not None else "no letter"
            extra = f", {s['errors']} failed (first: {s['first_error']})" if s["errors"] else ""
            fh.write(f"- **{model}**: {price}{extra}\n")
        fh.write("\nRead the pairs below and decide whether the difference shows in the writing.\n")
        for pair in pairs:
            job = pair["job"]
            fh.write(f"\n---\n\n## {job.get('company', '?')} — {job.get('title', '?')}\n\n")
            for model in models:
                d = pair["letters"][model]
                if d["error"]:
                    status = f"FAILED: {d['error']}"
                else:
                    status = f"${d['usd']:.5f}, {d['out']} output tokens"
                fh.write(f"### {model} — {status}\n\n```\n{d['text'].strip()}\n```\n\n")


def applied_postings(uid: str, n: int) -> list[dict]:
    """The postings behind this user's latest applications that carry a real description,
    one per company and title. Scoped by user_id: the service key bypasses RLS."""
    from app.db.client import get_supabase
    from app.routers.jobs import MIN_STORABLE_DESC

    res = (
        get_supabase()
        .table("applications")
        .select("job_title, company, jobs(title, company, description)")
        .eq("user_id", uid)
        .order("date_applied", desc=True)
        .limit(n * 10)
        .execute()
    )
    out, seen = [], set()
    for row in res.data or []:
        job = row.get("jobs") or {}
        description = (job.get("description") or "").strip()
        title = row.get("job_title") or job.get("title") or ""
        company = row.get("company") or job.get("company") or ""
        key = (company.lower(), title.lower())
        if len(description) < MIN_STORABLE_DESC or key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "company": company, "description": description})
        if len(out) == n:
            break
    return out


def load_user(uid: str, n: int) -> tuple[list[dict], dict, str, str]:
    """What /tools/cover-letter would use for this user: their profile and their resume."""
    from app.db.profile import get_profile
    from scripts.measure_judge_calibration import label_for, preflight_resume

    profile = get_profile(uid)
    preflight_resume(uid, profile)
    jobs = applied_postings(uid, n)
    if not jobs:
        raise SystemExit(f"{uid[:8]}: no recent application with a full posting description")
    resume = ai_cover_letter.resume_text_for(profile)
    return jobs, profile, resume, f"{label_for(profile)}, their latest applications"


def load_sample(path: str) -> tuple[list[dict], dict, str, str]:
    with open(path) as fh:
        jobs = json.load(fh)
    return jobs, SYNTHETIC_PROFILE, SYNTHETIC_RESUME, f"{os.path.basename(path)}, synthetic resume"


def main() -> int:
    # Measurement spend is not product spend: keep it out of the ledger (modules/ai_meter.py).
    # Set here, not on import, so a process that only imports these helpers keeps its meter.
    os.environ.setdefault("AI_METER", "off")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--user", help="user_id: their profile, resume and recent postings")
    source.add_argument("--sample", help="JSON list of {title, company, description}")
    ap.add_argument("--jobs", type=int, default=8, help="postings to take with --user")
    ap.add_argument(
        "--models", help=f"comma-separated, baseline first (default: shipped,{CANDIDATE})"
    )
    ap.add_argument("--out", default="letter_models.md")
    args = ap.parse_args()

    models = parse_models(args.models)
    loaded = load_user(args.user, args.jobs) if args.user else load_sample(args.sample)
    jobs, profile, resume, label = loaded
    print(f"{len(jobs)} postings ({label}) x {len(models)} models: {', '.join(models)}")

    client = ai_cover_letter.get_anthropic_client()
    pairs = write_letters(jobs, profile, resume, models, client)
    summary = summarize(pairs, models)
    print_summary(summary, models)
    write_markdown(pairs, summary, models, label, args.out)
    spent = sum(d["usd"] for p in pairs for d in p["letters"].values())
    print(f"\n  spent on this measurement: ${spent:.2f}\n  letters written to: {args.out}")
    return 1 if any(not summary[m]["letters"] for m in models) else 0


if __name__ == "__main__":
    raise SystemExit(main())
