#!/usr/bin/env python3
"""Offline coverage report for the ATS form filler.

Every filler bug so far cost a LIVE run to find, because there was nothing to test
against locally. `data/gh_form_schemas.jsonl` is 320 real Greenhouse application forms
(labels, required flags, field types, option values) — this walks all of them and asks
the question the unfilled-ledger answers only slowly and only in production:

    of the questions a real form REQUIRES, how many can we answer without asking an LLM,
    how many fall to the LLM, and how many would we leave blank and hand back?

The patterns below mirror content.js (deterministic layer: pickOptionDeterministic +
fillTextQuestions keyword map). They are a COPY, so treat a mismatch as a signal to
re-sync, not as ground truth about the running engine.

Usage:  python3 scripts/form_coverage.py [--misses N]
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

SCHEMAS = Path(__file__).resolve().parent.parent / "data" / "gh_form_schemas.jsonl"

# --- deterministic rules, mirroring content.js -------------------------------------
IDENTITY = re.compile(r"\b(first|last|full|your|legal|preferred)\s+name\b|^name$", re.I)
NOT_IDENTITY = re.compile(
    r"(reference|company|employer|supervisor|manager|contact|emergency|previous|prior)", re.I
)
# Non-English labels for the four fields every form requires (mirrors content.js
# NAME_I18N_RE / EMAIL_I18N_RE / PHONE_I18N_RE).
NAME_I18N = re.compile(
    r"^\s*(名|姓|氏名|お名前|이름|성|vorname|nachname|familienname|voornaam|achternaam|naam|prénom|prenom|nom de famille|nom|nombre|apellidos?|nome|sobrenome|imię|nazwisko|förnamn|efternamn|fornavn|etternavn|etunimi|sukunimi)\s*$",
    re.I,
)
EMAIL_I18N = re.compile(
    r"^\s*(電子メール|メールアドレス|이메일|e-?mail(adres|adresse)?|correo( electrónico)?|courriel|endereço de e-?mail)\s*$",
    re.I,
)
PHONE_I18N = re.compile(
    r"^\s*(電話|電話番号|전화번호|telefon(nummer)?|telefoon(nummer)?|téléphone|telefone|teléfono|puhelin)\s*$",
    re.I,
)

RULES = [
    (
        "name",
        lambda lb: (
            (bool(IDENTITY.search(lb)) and not NOT_IDENTITY.search(lb)) or bool(NAME_I18N.match(lb))
        ),
    ),
    ("email", lambda lb: bool(re.search(r"e-?\s?mail", lb)) or bool(EMAIL_I18N.match(lb))),
    ("phone", lambda lb: "phone" in lb or bool(PHONE_I18N.match(lb))),
    ("resume/file", lambda lb: bool(re.search(r"resume|cv\b|cover letter", lb))),
    ("linkedin/url", lambda lb: bool(re.search(r"linkedin|portfolio|website|github|\burl\b", lb))),
    (
        "address",
        lambda lb: (
            bool(re.search(r"\b(street|address ?line|address ?1|mailing address|zip|postal)\b", lb))
            or lb == "address"
        ),
    ),
    (
        "city/state",
        lambda lb: bool(re.search(r"\bcity\b|\bstate\b|\bprovince\b|\bregion\b|location", lb)),
    ),
    (
        "country/where",
        lambda lb: bool(
            re.search(
                r"(country|where (do|will) you (reside|live|work)|located in|intend to work)", lb
            )
        ),
    ),
    (
        "work auth",
        lambda lb: bool(re.search(r"(authoriz|eligible to work|legally.*work|right to work)", lb)),
    ),
    ("sponsorship", lambda lb: bool(re.search(r"(sponsor|visa\b|h-?1b|immigration)", lb))),
    (
        "salary",
        lambda lb: bool(re.search(r"(salary|compensation|pay range|wage|target|expected pay)", lb)),
    ),
    (
        "how did you hear",
        lambda lb: bool(
            re.search(
                r"how did you (hear|find|learn)|hear about (this|us|the)|where did you hear", lb
            )
        ),
    ),
    (
        "notice/start",
        lambda lb: bool(
            re.search(r"notice period|when (can|could) you start|available to start|start date", lb)
        ),
    ),
    (
        "english/lang",
        lambda lb: bool(re.search(r"(english|language).*(level|proficien|fluen)", lb)),
    ),
    (
        "demographic",
        lambda lb: bool(
            re.search(
                r"(gender|sex\b|race|ethnic|hispanic|latino|veteran|disab|pronoun|self.?identif|national origin)",
                lb,
            )
        ),
    ),
    (
        "consent/attest",
        lambda lb: bool(
            re.search(
                r"(certif|attest|acknowledge|consent|i have read|i understand|\bterms\b|agree)", lb
            )
        ),
    ),
    (
        "years/experience",
        lambda lb: bool(re.search(r"\byears?\b|experience|how many|how long", lb)),
    ),
    (
        "former employee",
        lambda lb: bool(
            re.search(
                r"(previously (worked|employed)|ever worked (at|for)|former (employee|employer)|currently employed by)",
                lb,
            )
        ),
    ),
    # Current employment — answered from profile.current_employer / current_title
    # (migrations/add_current_employment.sql). Placed after "former employee" so
    # were-you-ever-employed-here phrasings keep their rule. \bcurrent\b deliberately
    # does not match "currently" (relocation/relationship questions stay with the LLM),
    # and the guard drops questions ABOUT the employer that aren't its name/title
    # ("may we contact your current employer?", "how are you using AI in your role?").
    (
        "current employer/title",
        lambda lb: (
            bool(
                re.search(
                    r"\b(current|most recent|present)\b.*\b(employer|company|job title|title|position|role)\b",
                    lb,
                )
            )
            and not re.search(
                r"may we|contact|how (are|do|did)|using|why|describe|reflect|scope", lb
            )
        ),
    ),
    ("age/18", lambda lb: bool(re.search(r"\b18\b|over 18|age\b", lb))),
]
# Free-text questions the LLM answers (a real question, no keyword rule fits).
AI_WORTHY = re.compile(r"\?|why|describe|tell us|what (do|are|is)|explain|experience with", re.I)


def classify(label: str, ftype: str) -> str:
    lb = (label or "").strip().lower()
    if not lb:
        return "unlabelled"
    for name, fn in RULES:
        if fn(lb):
            return name
    # No deterministic rule. Long/question-shaped → LLM; otherwise we'd leave it blank.
    if AI_WORTHY.search(lb) or ftype in ("textarea",) or len(lb) > 20:
        return "→ AI"
    return "→ BLANK (hand-back)"


def main() -> int:
    want_misses = 25
    if "--misses" in sys.argv:
        want_misses = int(sys.argv[sys.argv.index("--misses") + 1])
    if not SCHEMAS.exists():
        print(f"missing {SCHEMAS}", file=sys.stderr)
        return 1

    forms = [json.loads(line) for line in SCHEMAS.open()]
    buckets: Counter = Counter()
    ai_labels: Counter = Counter()
    blank_labels: Counter = Counter()
    required_total = 0

    for form in forms:
        for q in form.get("questions", []):
            if not q.get("required"):
                continue
            required_total += 1
            ftype = (q.get("fields") or [{}])[0].get("type", "")
            verdict = classify(q.get("label", ""), ftype)
            buckets[verdict] += 1
            if verdict == "→ AI":
                ai_labels[(q.get("label") or "").strip().lower()[:70]] += 1
            elif verdict.startswith("→ BLANK"):
                blank_labels[(q.get("label") or "").strip().lower()[:70]] += 1

    det = sum(c for k, c in buckets.items() if not k.startswith("→") and k != "unlabelled")
    ai = buckets["→ AI"]
    blank = buckets["→ BLANK (hand-back)"] + buckets["unlabelled"]

    print(f"forms {len(forms)} · required questions {required_total}\n")
    print(f"  deterministic (no LLM)  {det:5}  {det / required_total * 100:5.1f}%")
    print(f"  → AI                    {ai:5}  {ai / required_total * 100:5.1f}%")
    print(f"  → blank / hand-back     {blank:5}  {blank / required_total * 100:5.1f}%\n")

    print("deterministic coverage by rule:")
    for name, count in buckets.most_common():
        if not name.startswith("→") and name != "unlabelled":
            print(f"  {name:20} {count:5}")

    print(f"\ntop {want_misses} questions that cost an LLM call (build handlers here first):")
    for label, count in ai_labels.most_common(want_misses):
        print(f"  {count:4}  {label}")

    if blank_labels:
        print("\ntop questions we would leave BLANK (→ hand-back):")
        for label, count in blank_labels.most_common(15):
            print(f"  {count:4}  {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
