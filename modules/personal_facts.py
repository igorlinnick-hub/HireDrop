"""What the person told us about their own circumstances — asked once, remembered, reused.

Employer forms keep asking things no resume states: "Are you willing to relocate to
Miami?", "Do you live within 30 miles of Austin?", "Can you work weekends?". The answerer
must not guess those (a "Yes" to relocating somewhere the person never meant to go is a
lie under their name), so until now such a question came back blank, the form was handed
back, and the person answered it inside that one application — and again on the next
form, because the answer was stored only on that job's hand-back row.

A fact is one answer the person gave, kept on their profile (`profiles.personal_facts`):

    {"id": "f_1a2b3c4d", "topic": "relocation",
     "question": "Are you willing to relocate to Miami, FL?", "answer": "No",
     "in_letters": false, "source": "popup", "updated_at": "2026-10-08T…"}

Three readers, one list:
  · the screener answerer — the same question again is answered from the fact with no
    model call; a related one ("relocate to San Diego?") is answered by the model from
    the facts, or comes back blank so the person is asked;
  · the cover-letter writer — facts marked `in_letters` ("Moving to San Diego in
    December") are mentioned where they matter for THIS job;
  · Drop — reads them, and proposes new ones behind a click.

Pure logic here; storage is app/db/personal_facts.py.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime

MAX_FACTS = 40
MAX_QUESTION = 300
MAX_ANSWER = 500

TOPIC_LABELS = {
    "relocation": "Relocation",
    "location": "Where you live",
    "onsite": "Working on-site",
    "travel": "Travel",
    "schedule": "Schedule and shifts",
    "start_date": "Start date",
    "other": "Other",
}

# A circumstance question is about the PERSON's life, not their experience. Order matters:
# the first topic that matches wins ("relocate to be on-site in Austin" is relocation).
_TOPICS: tuple[tuple[str, re.Pattern], ...] = (
    ("relocation", re.compile(r"\brelocat\w*|\b(?:move|moving)\s+to\b|\bwilling to move\b", re.I)),
    (
        "location",
        re.compile(
            r"\bwhere\b.{0,40}\b(?:live|living|reside|residing|located|based)\b"
            r"|\b(?:do|are|currently)\b.{0,30}\b(?:live|living|reside|residing|located|based)\b"
            r".{0,12}\b(?:in|near|within|around|close to)\b"
            r"|\blocal to\b|\bwithin (?:a )?\d+\s*-?\s*(?:miles?|mi|km)\b"
            r"|\bcommut(?:able|ing) distance\b",
            re.I,
        ),
    ),
    (
        "onsite",
        re.compile(r"\b(?:on-?site|in[- ]office|in[- ]person|in the office|hybrid)\b", re.I),
    ),
    ("travel", re.compile(r"\btravel(?:l?ing)?\b", re.I)),
    (
        "schedule",
        re.compile(
            r"\b(?:weekends?|overnights?|night shifts?|evening shifts?|shifts?|overtime|on-?call"
            r"|rotating schedule)\b",
            re.I,
        ),
    ),
    (
        "start_date",
        re.compile(
            r"\bwhen (?:can|could|would) you (?:start|begin)\b|\bstart date\b"
            r"|\bavailable to (?:start|begin)\b|\bnotice period\b|\bearliest (?:start|available)\b",
            re.I,
        ),
    ),
)

# Questions about EXPERIENCE that merely mention a topic word ("describe your experience
# working night shifts", "tell us about the travel industry") stay with the normal
# answerer: the resume answers them, not the person's circumstances.
_EXPERIENCE = re.compile(
    r"\b(?:describe|tell us|explain|walk us through|give an example|share an example"
    r"|experience (?:with|in|working)|how have you|why)\b",
    re.I,
)

# About THIS employer, not the person's life: "Have you applied to this company before?",
# "Are you willing to work in our Austin office?". The same words at the next employer
# ask something else, so an answer to one is never reused for another.
_THIS_EMPLOYER = re.compile(
    r"\b(?:this|our)\s+(?:[\w-]+\s+){0,2}"
    r"(?:company|organi[sz]ation|employer|role|position|team|job|opportunity|office|location)s?\b"
    r"|\b(?:with|for|join|joining) us\b",
    re.I,
)

_TRIM = re.compile(r"[\s*:：]+$|\(\s*required\s*\)\s*$", re.I)


def normalise(text: str) -> str:
    """Same rule as the screener cache (app/db/screener_cache._normalise): case,
    whitespace and trailing form decoration only. Anything looser would let the answer
    to "relocate to Miami?" answer "relocate to San Diego?"."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    return _TRIM.sub("", cleaned).strip().lower()


def topic_of(question: str) -> str | None:
    """The circumstance this question asks about, or None for anything else."""
    q = (question or "").strip()
    if not q or _EXPERIENCE.search(q):
        return None
    for topic, pattern in _TOPICS:
        if pattern.search(q):
            return topic
    return None


def about_this_employer(question: str) -> bool:
    return bool(_THIS_EMPLOYER.search(question or ""))


def reusable(question: str, options: list | None = None) -> bool:
    """Can an answer to this question be remembered and given to the NEXT employer? Yes for
    the person's circumstances and for fixed choices — never for a question about this one
    employer, or an open essay (its answer should differ per job)."""
    if not (question or "").strip() or about_this_employer(question):
        return False
    return bool(topic_of(question) or options)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return "f_" + secrets.token_hex(4)


def clean_fact(raw) -> dict | None:
    """One fact, bounded and typed; None when there is nothing to keep."""
    if not isinstance(raw, dict):
        return None
    question = re.sub(r"\s+", " ", str(raw.get("question") or "")).strip()[:MAX_QUESTION]
    answer = str(raw.get("answer") or "").strip()[:MAX_ANSWER]
    if not question or not answer:
        return None
    topic = str(raw.get("topic") or "").strip().lower()
    if topic not in TOPIC_LABELS:
        topic = topic_of(question) or "other"
    fid = str(raw.get("id") or "")
    if not re.fullmatch(r"f_[0-9a-f]{8}", fid):
        fid = new_id()
    return {
        "id": fid,
        "topic": topic,
        "question": question,
        "answer": answer,
        "in_letters": bool(raw.get("in_letters")),
        "source": str(raw.get("source") or "")[:20],
        "updated_at": str(raw.get("updated_at") or _now())[:40],
    }


def clean_facts(raw) -> list[dict]:
    """The stored list, cleaned: one fact per question (the newest wins), newest last,
    at most MAX_FACTS."""
    facts = [f for f in map(clean_fact, raw if isinstance(raw, list) else []) if f]
    by_question: dict[str, dict] = {}
    for f in sorted(facts, key=lambda f: f["updated_at"]):
        by_question[normalise(f["question"])] = f
    return list(by_question.values())[-MAX_FACTS:]


def upsert(
    facts: list[dict], fact: dict, replace_ids: list[str] | tuple = ()
) -> tuple[list[dict], dict]:
    """Add or update one fact. The same question replaces its earlier answer (one person,
    one answer to one question); `replace_ids` drops other facts the person chose to
    replace ("earlier you said San Diego — replace it with Miami?")."""
    fact = clean_fact({**fact, "updated_at": _now()})
    if not fact:
        raise ValueError("a fact needs a question and an answer")
    key = normalise(fact["question"])
    drop = set(replace_ids or ())
    kept = []
    for f in clean_facts(facts):
        if normalise(f["question"]) == key:
            fact["id"] = f["id"]  # same question, same fact: keep its id stable
            continue
        if f["id"] in drop:
            continue
        kept.append(f)
    return (kept + [fact])[-MAX_FACTS:], fact


def remove(facts: list[dict], fact_id: str) -> list[dict]:
    return [f for f in clean_facts(facts) if f["id"] != fact_id]


def related(facts: list[dict], topic: str | None, exclude_question: str = "") -> list[dict]:
    """Earlier answers on the same topic — shown next to a new question so the person
    sees what they said before ("earlier: San Diego") and can replace it."""
    if not topic or topic == "other":
        return []
    skip = normalise(exclude_question)
    return [
        f for f in clean_facts(facts) if f["topic"] == topic and normalise(f["question"]) != skip
    ]


_YES_NO = re.compile(r"^(yes|no)\b", re.I)


def snap(answer: str, options: list[str] | None) -> str | None:
    """The stored answer as one of this form's options, or None when it doesn't fit.

    Exact (case/space-insensitive) first; then a bare Yes/No onto the one option that
    starts with it ("Yes" → "Yes, I am willing to relocate"). Never anything looser: a
    wrong pick is worse than asking."""
    answer = (answer or "").strip()
    if not answer:
        return None
    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    if not opts:
        return answer
    low = answer.lower()
    for o in opts:
        if o.lower() == low:
            return o
    m = _YES_NO.match(answer)
    if m:
        word = m.group(1).lower()
        hits = [o for o in opts if (_YES_NO.match(o) or [None, ""])[1].lower() == word]
        if len(hits) == 1:
            return hits[0]
    return None


def match(question: str, options: list[str] | None, facts: list[dict]) -> dict | None:
    """The fact that answers exactly this question (same normalised wording), with the
    answer snapped to this form's options; None when there is none or it doesn't fit."""
    want = normalise(question)
    if not want or about_this_employer(question):
        return None
    for f in clean_facts(facts):
        if normalise(f["question"]) != want:
            continue
        picked = snap(f["answer"], options)
        if picked is None:
            return None
        return {**f, "answer": picked}
    return None


def lines(facts: list[dict], only_letters: bool = False) -> str:
    """Facts as prompt lines: "- <question>: <answer>"."""
    return "\n".join(
        f"- {f['question']}: {f['answer']}"
        for f in clean_facts(facts)
        if f["in_letters"] or not only_letters
    )


def fingerprint(facts: list[dict]) -> str:
    """Short hash of what the facts say — part of the screener cache key, so a changed
    answer retires every cached answer derived from the old one. "" when there are none
    (the key then stays what it was before facts existed)."""
    pairs = sorted((normalise(f["question"]), f["answer"]) for f in clean_facts(facts))
    if not pairs:
        return ""
    return hashlib.sha256(repr(pairs).encode()).hexdigest()[:12]
