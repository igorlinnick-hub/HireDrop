#!/usr/bin/env python3
"""Put two cover-letter models side by side on the SAME jobs — cost and text.

Written 2026-09-21 to settle whether tap should keep the cheaper letter model. It did
settle it (it shouldn't — the split is gone, see COVER_LETTER_MODEL), and the script
stays because the question recurs: any time a cheaper or newer model shows up, this is
how you find out what it actually costs and what it actually writes.

measure_ai_cost.py can't answer this: it prices ONE letter per job, with whatever model
ships. Judging a model swap needs both letters for the SAME posting — same prompt, same
resume — so the model is the only variable. Edit MODELS below to compare a new pair.

What the first run found, and why the text half matters as much as the numbers: Sonnet
opened 5 of 8 letters with "Here's a cover letter for Jordan:" and a --- fence, which
went into the employer's form verbatim. Four such letters were already in the production
applications table. That bug was invisible to every cost measurement ever run here.

This writes two things:
  * the numbers — measured input/output tokens and dollars per letter, per model, and
    what the delta does to the cost of an application;
  * the letters themselves, side by side in a markdown file, because "is Sonnet actually
    better here" is a judgement a human makes by reading, not a number.

USAGE
  # sample.json: [{"title":…, "company":…, "description":…}, …]
  .venv/bin/python scripts/measure_letter_models.py sample.json [out.md]

It spends real money: two letters per job, roughly $0.01 per job at current prices.
Keep samples small. The resume is SYNTHETIC (same one measure_ai_cost.py uses) — cost and
prompt shape don't depend on whose career it is.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules import ai_cover_letter  # noqa: E402

# $ per million tokens (input, output) — Anthropic list prices, same table as
# measure_ai_cost.py. Keep the two in sync when prices move.
PRICES = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}

# What one application costs today, measured 2026-09-15 (scripts/measure_ai_cost.py).
# Used only to express the letter delta as a share of the whole — the letter numbers
# below are measured fresh on every run.
BASELINE_APP_USD = 0.0129

# Which models to put side by side. Keys are just labels for the report; "tap"/"auto"
# are kept so the output reads the same as the 09-21 measurement it is compared against.
MODELS = {
    "tap": "claude-haiku-4-5-20251001",
    "auto": "claude-sonnet-4-6",
}

CALLS: list[dict] = []


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

BASE_PROFILE = {
    "name": "Jordan",
    "last_name": "Avery",
    "email": "jordan.avery@example.com",
    "apply_mode": "standard",
    "resume_url": None,
    "keywords": ["project manager"],
}


def main(path: str, out_path: str = "letter_models.md") -> int:
    with open(path) as fh:
        jobs = json.load(fh)
    _install_recorder()
    ai_cover_letter.load_resume_text = lambda *a, **k: RESUME
    original_model = ai_cover_letter.COVER_LETTER_MODEL

    rows = []
    pairs = []
    for i, job in enumerate(jobs, 1):
        letters = {}
        for mode, model in MODELS.items():
            mark = len(CALLS)
            # The model is no longer chosen by submit_mode (that split was dropped on
            # 2026-09-21 — see COVER_LETTER_MODEL). To compare models this now sets the
            # module constant directly, which is also what makes the script useful for
            # ANY future candidate model, not just the two that used to be wired to modes.
            ai_cover_letter.COVER_LETTER_MODEL = model
            text = ai_cover_letter.generate_cover_letter(job, BASE_PROFILE)
            calls = CALLS[mark:]
            if not calls:
                print(f"  !! {mode}: no API call recorded (fell back to the template?)")
                continue
            letters[mode] = {
                "text": text,
                "usd": sum(c["usd"] for c in calls),
                "in": sum(c["in"] for c in calls),
                "out": sum(c["out"] for c in calls),
                "model": calls[-1]["model"],
                "chars": len(text),
            }
        if len(letters) != 2:
            continue
        rows.append(letters)
        pairs.append((job, letters))
        t, a = letters["tap"], letters["auto"]
        print(
            f"  {i:2}. {(job.get('company') or '?')[:18]:<18} "
            f"haiku ${t['usd']:.5f} ({t['out']:>3} out, {t['chars']:>4}ch)   "
            f"sonnet ${a['usd']:.5f} ({a['out']:>3} out, {a['chars']:>4}ch)"
        )

    ai_cover_letter.COVER_LETTER_MODEL = original_model

    if not rows:
        print("Nothing measured — did the API key resolve?")
        return 1

    n = len(rows)
    haiku = sum(r["tap"]["usd"] for r in rows) / n
    sonnet = sum(r["auto"]["usd"] for r in rows) / n
    delta = sonnet - haiku

    print("\n" + "=" * 72)
    print("MEASURED — real usage blocks, both models on the same postings")
    print("=" * 72)
    for label, key in (("Haiku ", "tap"), ("Sonnet", "auto")):
        avg_in = sum(r[key]["in"] for r in rows) / n
        avg_out = sum(r[key]["out"] for r in rows) / n
        avg_ch = sum(r[key]["chars"] for r in rows) / n
        usd = haiku if key == "tap" else sonnet
        print(
            f"  {label:<22} in {avg_in:>6.0f}  out {avg_out:>5.0f}  "
            f"{avg_ch:>5.0f} chars  ${usd:.5f}/letter"
        )
    print()
    print(f"  letters measured            {n}")
    print(f"  difference per letter       ${delta:.5f}  ({sonnet / haiku:.1f}x)")
    print()
    print("  What the difference is worth per application:")
    print(f"    today (tap, Haiku letter)   ${BASELINE_APP_USD - delta:.4f}  approx")
    print(f"    if tap moved to Sonnet      ${BASELINE_APP_USD:.4f}  = what auto costs now")
    print(f"    delta per application       ${delta:.5f}  ({delta / BASELINE_APP_USD:.1%} of it)")
    print(f"    at the 30/day cap           ${delta * 30:.4f}/day, ${delta * 30 * 30:.2f}/month")
    print()
    print(f"  spent running this measurement: ${sum(c['usd'] for c in CALLS):.2f}")

    with open(out_path, "w") as fh:
        fh.write("# Cover letter: Haiku (tap) vs Sonnet (auto), same postings\n\n")
        fh.write(
            f"{n} postings, one letter from each model. Same prompt, same synthetic resume — "
            "the model is the only variable.\n\n"
        )
        fh.write(
            f"- Haiku: **${haiku:.5f}**/letter · Sonnet: **${sonnet:.5f}**/letter · "
            f"difference **${delta:.5f}** ({sonnet / haiku:.1f}x)\n"
            f"- Per application that is **{delta / BASELINE_APP_USD:.1%}** of today's "
            f"${BASELINE_APP_USD:.4f}; at the 30/day cap, **${delta * 30 * 30:.2f}/month**.\n\n"
            "Read the pairs below and decide whether the difference is visible in the writing.\n"
        )
        for job, letters in pairs:
            fh.write(f"\n---\n\n## {job.get('company', '?')} — {job.get('title', '?')}\n\n")
            for label, key in (("Haiku", "tap"), ("Sonnet (what ships today)", "auto")):
                d = letters[key]
                fh.write(f"### {label} — ${d['usd']:.5f}, {d['out']} output tokens\n\n")
                fh.write("```\n" + d["text"].strip() + "\n```\n\n")
    print(f"  letters written to: {out_path}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "letter_models.md"))
