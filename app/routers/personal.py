"""Facts about the person, and the questions only they can answer — asked once, remembered.

  GET    /profile/facts            what they told us (modules/personal_facts.py)
  POST   /profile/facts            remember one answer — and put it on every open hand-back
                                   that asked the same question (re-queued when nothing
                                   else is missing)
  DELETE /profile/facts/{id}       forget one
  GET    /personal-questions       questions waiting on the person, from open hand-backs,
                                   one entry per question however many jobs ask it, with
                                   what they said before on the same topic

Every surface that asks (extension popup, History, Drop's cards) calls POST /profile/facts,
so "answered once" means the same thing everywhere. Drop never calls it: it proposes a
card, and the person's click sends this request.
"""

from __future__ import annotations

import sys

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db import handbacks as handbacks_db
from app.db import jobs as jobs_db
from app.db import personal_facts as facts_db
from app.deps import get_current_user
from modules import personal_facts as pf

router = APIRouter(tags=["personal"])

QUESTIONS_LIMIT = 10


class FactBody(BaseModel):
    question: str = Field(..., min_length=1, max_length=pf.MAX_QUESTION)
    answer: str = Field(..., min_length=1, max_length=pf.MAX_ANSWER)
    topic: str | None = Field(None, max_length=20)
    # Mention it in cover letters where it fits the job ("Moving to San Diego in December").
    in_letters: bool = False
    # Earlier facts this answer replaces ("earlier: San Diego — replace with Miami").
    replace_ids: list[str] = Field(default_factory=list, max_length=10)
    # Where it was answered — popup | history | drop | settings. Measurement only.
    source: str = Field("dashboard", max_length=20)


def _waiting_questions(rows: list[dict], facts: list[dict]) -> list[dict]:
    """One entry per question the person still has to answer, newest hand-back first.

    Only questions an answer can be REUSED for: a circumstance (relocate, live near,
    on-site, travel, shifts, start date) or a fixed choice. An open essay about one job
    ("Why this company?") stays in History next to its job — remembering it would send the
    same paragraph to the next employer.
    """
    items: dict[str, dict] = {}
    for r in rows:
        answered = {pf.normalise(k) for k in (r.get("answers") or {})}
        for q in r.get("questions") or []:
            label = (q or {}).get("label") or ""
            options = (q or {}).get("options") or []
            key = pf.normalise(label)
            if not key or key in answered:
                continue
            if pf.match(label, options, facts):
                continue  # the memory answers it now — the retry will fill it
            topic = pf.topic_of(label)
            if not topic and not options:
                continue
            item = items.get(key)
            if item is None:
                item = items[key] = {
                    "question": label,
                    "options": options,
                    "topic": topic or "other",
                    "topic_label": pf.TOPIC_LABELS[topic or "other"],
                    "related": pf.related(facts, topic, label),
                    "jobs": [],
                    "last_seen": r.get("created_at"),
                }
            item["jobs"].append(
                {
                    "handback_id": r.get("id"),
                    "title": r.get("job_title") or "",
                    "company": r.get("company") or "",
                    "platform": r.get("platform") or "",
                }
            )
    # Most-blocking first (one answer frees the most jobs), then the most recent.
    out = sorted(items.values(), key=lambda i: i["last_seen"] or "", reverse=True)
    return sorted(out, key=lambda i: -len(i["jobs"]))


def waiting_questions(user_id: str, limit: int = QUESTIONS_LIMIT) -> list[dict]:
    rows = handbacks_db.list_open(user_id, limit=100)
    return _waiting_questions(rows, facts_db.get(user_id))[:limit]


def apply_to_handbacks(user_id: str, fact: dict, facts: list[dict]) -> dict:
    """Put a remembered answer on every open hand-back that asked this question.

    A hand-back whose questions are now ALL answered (by the person on that row, or by
    their facts) goes back to the queue — same as answering it in History. One that still
    misses something keeps the answer and waits: re-queueing it would only hit the same
    blank field again.
    """
    key = pf.normalise(fact["question"])
    requeued = waiting = 0
    for r in handbacks_db.list_open(user_id, limit=100):
        questions = r.get("questions") or []
        asked = next((q for q in questions if pf.normalise(q.get("label") or "") == key), None)
        if not asked:
            continue
        picked = pf.snap(fact["answer"], asked.get("options") or [])
        if picked is None:
            continue  # this form's choices don't fit the answer — the person decides there
        answers = dict(r.get("answers") or {})
        answers[asked["label"]] = picked
        complete = True
        for q in questions:
            label = q.get("label") or ""
            if any(pf.normalise(k) == pf.normalise(label) for k in answers):
                continue
            known = pf.match(label, q.get("options") or [], facts)
            if known:
                answers[label] = known["answer"]
            else:
                complete = False
        if complete:
            handbacks_db.save_answers(user_id, r["id"], answers)
            if r.get("job_id"):
                jobs_db.update_job_status(user_id, r["job_id"], "approved")
            requeued += 1
        else:
            handbacks_db.store_answers(user_id, r["id"], answers)
            waiting += 1
    return {"requeued": requeued, "still_waiting": waiting}


def remember(user_id: str, body: FactBody) -> dict:
    """Save one answer and apply it. Raises 503 before the migration, 400 on a choice
    the waiting forms can't take."""
    # A question with fixed choices must be answered with one of them, or no form that
    # asked it can be filled from the answer.
    waiting = [
        q
        for q in waiting_questions(user_id, limit=100)
        if pf.normalise(q["question"]) == pf.normalise(body.question)
    ]
    answer = body.answer.strip()
    if waiting and waiting[0]["options"]:
        picked = pf.snap(answer, waiting[0]["options"])
        if picked is None:
            raise HTTPException(
                status_code=400,
                detail={"error": "pick_an_option", "options": waiting[0]["options"]},
            )
        answer = picked
    try:
        facts, fact = facts_db.upsert(
            user_id,
            {
                "question": body.question,
                "answer": answer,
                "topic": body.topic,
                "in_letters": body.in_letters,
                "source": body.source,
            },
            body.replace_ids,
        )
    except facts_db.FactsUnavailableError as e:
        raise HTTPException(status_code=503, detail="facts_not_ready") from e
    try:
        applied = apply_to_handbacks(user_id, fact, facts)
    except Exception as e:  # noqa: BLE001 — the answer is saved; the retry still uses it
        print(f"[facts] applying to hand-backs failed: {e}", file=sys.stderr)
        applied = {"requeued": 0, "still_waiting": 0}
    return {"ok": True, "fact": fact, "facts": facts, **applied}


@router.get("/profile/facts")
def list_facts(user=Depends(get_current_user)):
    return {"facts": facts_db.get(user.id), "topics": pf.TOPIC_LABELS}


@router.post("/profile/facts")
def save_fact(body: FactBody, user=Depends(get_current_user)):
    return remember(user.id, body)


@router.delete("/profile/facts/{fact_id}")
def delete_fact(fact_id: str, user=Depends(get_current_user)):
    try:
        facts = facts_db.remove(user.id, fact_id)
    except facts_db.FactsUnavailableError as e:
        raise HTTPException(status_code=503, detail="facts_not_ready") from e
    return {"ok": True, "facts": facts}


@router.get("/personal-questions")
def personal_questions(user=Depends(get_current_user), limit: int = QUESTIONS_LIMIT):
    return {"questions": waiting_questions(user.id, limit=min(max(limit, 1), 50))}
