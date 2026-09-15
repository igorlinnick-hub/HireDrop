#!/usr/bin/env python3
"""Measure what an application ACTUALLY costs, in tokens, against real jobs.

Every cost figure this project has argued over was an estimate read off prompt
sizes: $0.007 in memory, ~$0.03 in a code comment, $0.039 from a spreadsheet.
They disagreed by 5x and the argument was unresolvable, because the `usage` block
Anthropic returns on every response was never recorded anywhere.

This script records it. It wraps the Anthropic client, runs the real modules over
real job rows, and reports measured input/output tokens and dollars per call —
plus the cascade's escalation rate, which is the number FIT_CASCADE_BAND should be
tuned on and which nothing else measures.

USAGE
  # 1. export a representative sample of jobs (no length filter: the judge runs
  #    on every candidate, and ~45% of the pool has no description at all)
  supabase db query --linked -f - <<'SQL' > sample.json
  select json_agg(row_to_json(t)) from (
    select title, company, coalesce(description,'') as description
    from jobs order by md5(id::text) limit 20) t;
  SQL
  # 2. measure
  .venv/bin/python scripts/measure_ai_cost.py sample.json

It spends real money — roughly $0.02 per job. Keep samples small.

The resume is SYNTHETIC by design: cost depends on token count, not on whose
career it is, so there is no reason to pull a real user's document into a
benchmark. It is padded to the same 3000-char cap load_resume_text applies.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules import ai_cover_letter, ai_fit_judge  # noqa: E402

# $ per million tokens, Anthropic list prices.
PRICES = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}

CALLS: list[dict] = []

# Measure the letter on at least this many jobs even when they all get skipped.
LETTER_FLOOR = 4


def _install_recorder():
    """Wrap the shared client so every call records its real usage block."""
    real = ai_cover_letter.get_anthropic_client()

    class Recorder:
        def create(self, **kwargs):
            message = real.messages.create(**kwargs)
            usage = message.usage
            model = kwargs["model"]
            price_in, price_out = PRICES.get(model, (0.0, 0.0))
            CALLS.append(
                {
                    "model": model,
                    "in": usage.input_tokens,
                    "out": usage.output_tokens,
                    "usd": (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1e6,
                }
            )
            return message

    client = type("C", (), {"messages": Recorder()})()
    ai_cover_letter.get_anthropic_client = lambda: client
    ai_fit_judge.get_anthropic_client = lambda: client


# 3000 chars — exactly what load_resume_text() would hand the prompt.
RESUME = (
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
) * 3
RESUME = RESUME[:3000]

PROFILE = {
    "name": "Jordan",
    "last_name": "Avery",
    "email": "jordan.avery@example.com",
    "apply_mode": "standard",
    "resume_url": None,
    "keywords": ["project manager"],
}


def main(path: str) -> int:
    with open(path) as fh:
        jobs = json.load(fh)
    _install_recorder()
    # Bypass storage: the synthetic resume IS the input under test.
    ai_cover_letter.load_resume_text = lambda *a, **k: RESUME
    ai_fit_judge.load_resume_text = lambda *a, **k: RESUME

    escalated = cheap_decided = 0
    per_job = []
    lettered = 0

    for i, job in enumerate(jobs, 1):
        before = len(CALLS)
        verdict = ai_fit_judge.assess_fit(job=job, profile=PROFILE)
        judge_calls = CALLS[before:]
        if verdict.get("escalated"):
            escalated += 1
        elif verdict.get("judge_model") == ai_fit_judge._SCREEN_MODEL:
            cheap_decided += 1

        # Price the letter for jobs that passed the bar — plus, if a sample skips
        # everything (easy on a random pool slice), for the first few anyway. The
        # letter's cost does not depend on the verdict, and a run that measures no
        # letter at all reports a per-application total that is missing a term.
        letter_calls = []
        if verdict["decision"] == "apply" or i <= LETTER_FLOOR:
            mark = len(CALLS)
            ai_cover_letter.generate_cover_letter(job, PROFILE)
            letter_calls = CALLS[mark:]
            lettered += 1

        per_job.append(
            {
                "n": i,
                "desc_chars": len(job.get("description") or ""),
                "score": verdict["fit_score"],
                "decision": verdict["decision"],
                "judge": "escalated"
                if verdict.get("escalated")
                else verdict.get("judge_model", "-"),
                "judge_usd": sum(c["usd"] for c in judge_calls),
                "letter_usd": sum(c["usd"] for c in letter_calls),
            }
        )
        print(
            f"  {i:2}. desc {per_job[-1]['desc_chars']:>5}ch  score {verdict['fit_score']:>3}  "
            f"{verdict['decision']:<5} {per_job[-1]['judge']:<30} "
            f"judge ${per_job[-1]['judge_usd']:.4f}  letter ${per_job[-1]['letter_usd']:.4f}"
        )

    print("\n" + "=" * 72)
    print("MEASURED — real usage blocks, not prompt-size arithmetic")
    print("=" * 72)
    by_model: dict[str, list[dict]] = {}
    for c in CALLS:
        by_model.setdefault(c["model"], []).append(c)
    for model, calls in sorted(by_model.items()):
        n = len(calls)
        print(
            f"  {model:<30} {n:>3} calls  "
            f"in {sum(c['in'] for c in calls) / n:>7.0f}  out {sum(c['out'] for c in calls) / n:>6.0f}  "
            f"${sum(c['usd'] for c in calls) / n:.4f}/call"
        )

    applied = [j for j in per_job if j["decision"] == "apply"]
    total_judge = sum(j["judge_usd"] for j in per_job)
    total_letter = sum(j["letter_usd"] for j in per_job)
    print()
    print(f"  jobs judged                {len(per_job)}")
    print(f"  decided by the cheap model {cheap_decided}  ({cheap_decided / len(per_job):.0%})")
    print(f"  escalated to Sonnet        {escalated}  ({escalated / len(per_job):.0%})")
    print(f"  passed the bar (applied)   {len(applied)}")
    print()
    print(f"  judging, per job judged    ${total_judge / len(per_job):.4f}")
    if lettered:
        print(f"  cover letter, per letter   ${total_letter / lettered:.4f}  ({lettered} measured)")
    if applied:
        ratio = len(per_job) / len(applied)
        print(
            f"  judging, per APPLICATION   ${total_judge / len(applied):.4f}"
            f"   ({ratio:.1f} jobs judged per application)"
        )
    else:
        print("  judging per APPLICATION    n/a — nothing in this sample passed the bar,")
        print("                             so the jobs-judged-per-application ratio is")
        print("                             unmeasured here. It needs a live campaign.")
    print("  (screener answers not included — they need a live form)")
    print(f"\n  spent running this measurement: ${sum(c['usd'] for c in CALLS):.2f}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
