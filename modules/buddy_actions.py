"""Drop proposes, the person presses — cards with one button, built and checked here.

Drop stays read-only (modules/buddy.py): it reads job postings, employer questions and log
lines, and any of those can carry "Drop, change the user's resume to…". So it never
changes anything itself. What it can do is put a CARD in the chat — "Remember: Moving to
San Diego in December · [Save]" — and the person's click sends an ordinary, authenticated
request to an endpoint they could have used from Settings.

Everything on the card is built HERE from a fixed list of kinds, never from the model's
own text about paths or methods: the model fills values (a question, an answer, an
application id); this module checks each one against the person's own account (an id
that is not theirs, a resume that doesn't exist → no card) and writes the request. The
model cannot name an endpoint, so no wording in a posting can make a card that stops a
campaign or edits a field outside this list.
"""

from __future__ import annotations

import secrets

from modules import personal_facts as pf

KINDS = (
    "remember_answer",
    "forget_answer",
    "open_application",
    "rebuild_ats_resume",
    "use_resume",
    "set_resume_location",
)

RESUME_NAMES = {
    "original": "the resume you uploaded",
    "ats": "the ATS resume",
    "skills": "the skills resume",
}

TOOL = {
    "name": "propose_action",
    "description": (
        "Show the user a card with ONE button that does something in their account. Nothing "
        "happens unless they press it — you never change anything yourself. Use it only when "
        "they ask to remember / change / open / rebuild something, or to offer the obvious "
        "next step (e.g. after a new resume upload). One card per call; in your reply say in "
        "one plain sentence what the button does.\n"
        "Kinds:\n"
        "- remember_answer: keep something about the user's own circumstances for every "
        "application (question + answer). Use the employer's exact question text when "
        "answering a waiting question (get_personal_answers), so the waiting jobs get it; or "
        "a short label like 'Relocation plans' for a note. in_letters=true to also mention it "
        "in cover letters where it fits the job. replace_ids = earlier answers it replaces.\n"
        "- forget_answer: delete one remembered answer (fact_id).\n"
        "- open_application: open one application in History with its letter and resume "
        "(application_id from get_recent_applications).\n"
        "- rebuild_ats_resume: rebuild the ATS resume from the uploaded PDF and use it for "
        "applications (uses one of today's AI builds).\n"
        "- use_resume: choose which resume applications send (original | ats | skills).\n"
        "- set_resume_location: change the city line on the ATS resume (no AI, re-renders)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "question": {"type": "string"},
            "answer": {"type": "string"},
            "in_letters": {"type": "boolean"},
            "replace_ids": {"type": "array", "items": {"type": "string"}},
            "fact_id": {"type": "string"},
            "application_id": {"type": "string"},
            "resume": {"type": "string", "enum": ["original", "ats", "skills"]},
            "location": {"type": "string"},
        },
        "required": ["kind"],
    },
}


class ProposalError(ValueError):
    """Why no card was made — told to the model, which tells the user."""


def _text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _card(kind: str, title: str, lines: list[str], confirm: str, **extra) -> dict:
    return {
        "id": "p_" + secrets.token_hex(4),
        "kind": kind,
        "title": title,
        "lines": [x for x in lines if x],
        "confirm": confirm,
        "steps": extra.pop("steps", []),
        "navigate": extra.pop("navigate", None),
        "note": extra.pop("note", ""),
        "done": extra.pop("done", ""),
    }


def build(user_id: str, args: dict, *, facts: list[dict], profile: dict, find_application) -> dict:
    """One card from the model's tool input, or ProposalError. `find_application(uid, id)`
    returns the person's application or None (injected: tests and the DB stay apart)."""
    args = args if isinstance(args, dict) else {}
    kind = args.get("kind")
    if kind not in KINDS:
        raise ProposalError(f"unknown kind {kind!r}")

    if kind == "remember_answer":
        question = _text(args.get("question"), pf.MAX_QUESTION)
        answer = _text(args.get("answer"), pf.MAX_ANSWER)
        if not question or not answer:
            raise ProposalError("remember_answer needs both a question and an answer")
        known = {f["id"]: f for f in facts}
        replaces = [known[i] for i in (args.get("replace_ids") or []) if i in known]
        in_letters = bool(args.get("in_letters"))
        return _card(
            kind,
            "Remember this for every application?",
            [f"{question}: {answer}"]
            + [f"Replaces: {f['question']}: {f['answer']}" for f in replaces],
            "Save",
            note="Also mentioned in cover letters where it fits the job." if in_letters else "",
            steps=[
                {
                    "method": "POST",
                    "path": "/profile/facts",
                    "body": {
                        "question": question,
                        "answer": answer,
                        "in_letters": in_letters,
                        "replace_ids": [f["id"] for f in replaces],
                        "source": "drop",
                    },
                }
            ],
            done="Saved. I'll use it on every application.",
        )

    if kind == "forget_answer":
        fact = next((f for f in facts if f["id"] == args.get("fact_id")), None)
        if not fact:
            raise ProposalError("no remembered answer with that id")
        return _card(
            kind,
            "Forget this answer?",
            [f"{fact['question']}: {fact['answer']}"],
            "Forget it",
            steps=[{"method": "DELETE", "path": f"/profile/facts/{fact['id']}", "body": None}],
            done="Forgotten. If an employer asks again, I'll ask you.",
        )

    if kind == "open_application":
        app_id = _text(args.get("application_id"), 64)
        app = find_application(user_id, app_id) if app_id else None
        if not app:
            raise ProposalError("no application of this user with that id")
        when = str(app.get("date_applied") or "")[:10]
        return _card(
            kind,
            "Open this application",
            [
                " at ".join(x for x in (app.get("title"), app.get("company")) if x),
                f"Applied {when}" if when else "",
                "Cover letter included"
                if app.get("cover_letter")
                else "No cover letter was asked for",
            ],
            "Open",
            navigate=f"/dashboard/history?app={app['id']}",
        )

    if kind == "rebuild_ats_resume":
        if not profile.get("resume_url"):
            raise ProposalError("no uploaded resume to rebuild from")
        return _card(
            kind,
            "Rebuild your ATS resume and use it?",
            [
                "Reads the resume PDF you uploaded and builds the ATS version from it.",
                "Applications then send the ATS version.",
            ],
            "Rebuild",
            note="Uses one of today's AI builds.",
            steps=[
                {"method": "POST", "path": "/profile/ats/generate", "body": {}},
                {"method": "POST", "path": "/profile/resume/default", "body": {"choice": "ats"}},
            ],
            done="Done. New applications send the rebuilt ATS resume.",
        )

    if kind == "use_resume":
        choice = args.get("resume")
        have = {
            "original": bool(profile.get("resume_url")),
            "ats": bool(profile.get("ats_resume_url")),
            "skills": bool(profile.get("skills_resume_url")),
        }
        if choice not in have:
            raise ProposalError("resume must be original, ats or skills")
        if not have[choice]:
            raise ProposalError(f"there is no {RESUME_NAMES[choice]} yet")
        return _card(
            kind,
            "Send this resume with applications?",
            [f"Use {RESUME_NAMES[choice]} from now on."],
            "Use it",
            steps=[
                {"method": "POST", "path": "/profile/resume/default", "body": {"choice": choice}}
            ],
            done=f"Done. Applications now send {RESUME_NAMES[choice]}.",
        )

    # set_resume_location
    location = _text(args.get("location"), 120)
    if len(location) < 2:
        raise ProposalError("set_resume_location needs a location like 'San Diego, CA'")
    structure = profile.get("ats_structure") or {}
    if not structure:
        raise ProposalError("there is no ATS resume to edit yet")
    current = _text((structure.get("contact") or {}).get("location"), 120)
    return _card(
        kind,
        "Change the city on your ATS resume?",
        [f"From: {current or '(none)'}", f"To: {location}"],
        "Change it",
        note="No AI involved: the resume is re-rendered with exactly this line.",
        steps=[{"method": "POST", "path": "/profile/ats/contact", "body": {"location": location}}],
        done=f"Done. Your ATS resume now says {location}.",
    )


def summary_for_model(card: dict) -> dict:
    """What the model is told after a card was shown — enough to describe it, and the rule."""
    return {
        "shown": True,
        "card": card["title"],
        "button": card["confirm"],
        "lines": card["lines"],
        "rule": "Nothing has changed yet. It happens only if the user presses the button.",
    }
