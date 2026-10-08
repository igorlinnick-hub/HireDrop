#!/usr/bin/env python3
"""Who is working on what: one claim file per live Claude session, read by every other one.

Several sessions run in parallel (ext lane, web app, ads, handbacks…). Each one is lost to
the others unless it says, in a place they all read: my name, my lane, my goal, what I am
doing now, which files I touch. This is that place — `docs/sessions/<name>.md`, one file per
session (one file each, so two sessions never edit the same file and git never conflicts).

    python3 scripts/sessions.py board                     # who is live, stale, overlapping
    python3 scripts/sessions.py claim --lane ext --goal "ZR submits recorded in History" \\
        --now "finish .wt-zr-truth into a PR" --scope chrome-extension/content.js
    python3 scripts/sessions.py beat --now "PR #395 open, CI running"
    python3 scripts/sessions.py done                      # the lane is finished: drop the claim
    python3 scripts/sessions.py hook                      # SessionStart hook (.claude/settings.json)

The name is NOT chosen by anyone: it is derived from the Claude session id (adjective-noun),
so the hook announces it before the first prompt and it stays the same across /compact.
claim/beat/done need `--session <id>` (the hook prints it) or $CLAUDE_SESSION_ID — never a
"last seen id" file: sessions share one working tree, and a shared file would point at whoever
started last.

Rules the file encodes (from claims-with-lease practice, Anthropic's long-running harness):
- goal = the END state in one line (what "done" means), now = the current step. After a
  compaction the hook prints them back, so a session that drifted can return.
- a claim not touched for STALE_HOURS is stale: its lane is free to take over (`claim` on the
  same lane replaces it and says so). A claim is a lease, not a lock.
- scope overlap with another live claim is reported loudly: that is the "two sessions in one
  content.js" accident CLAUDE.md warns about.
"""

import argparse
import fnmatch
import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = ROOT / "docs" / "sessions"
STALE_HOURS = 8
FIELDS = ("name", "session_id", "lane", "goal", "now", "scope", "branch", "started", "updated")

ADJECTIVES = [
    "amber",
    "azure",
    "bold",
    "brisk",
    "calm",
    "cedar",
    "clever",
    "coral",
    "crisp",
    "dawn",
    "deft",
    "eager",
    "ember",
    "fern",
    "frost",
    "gentle",
    "golden",
    "granite",
    "hazel",
    "ivory",
    "jade",
    "keen",
    "lunar",
    "maple",
    "mellow",
    "misty",
    "noble",
    "ochre",
    "olive",
    "pine",
    "plum",
    "quiet",
    "rapid",
    "russet",
    "sage",
    "silver",
    "slate",
    "solar",
    "steady",
    "swift",
    "teal",
    "tidal",
    "topaz",
    "velvet",
    "vivid",
    "warm",
    "willow",
]
NOUNS = [
    "badger",
    "bison",
    "crane",
    "falcon",
    "fox",
    "gecko",
    "heron",
    "ibis",
    "jaguar",
    "kestrel",
    "koala",
    "lark",
    "lemur",
    "lynx",
    "marten",
    "merlin",
    "moose",
    "newt",
    "ocelot",
    "orca",
    "osprey",
    "otter",
    "owl",
    "panda",
    "puffin",
    "quail",
    "raven",
    "robin",
    "salmon",
    "seal",
    "shrike",
    "sparrow",
    "stoat",
    "swift",
    "tapir",
    "tern",
    "thrush",
    "tiger",
    "toucan",
    "trout",
    "viper",
    "walrus",
    "wolf",
    "wren",
    "yak",
    "zebra",
]


def name_for(session_id: str) -> str:
    """Stable adjective-noun name for a Claude session id."""
    digest = hashlib.sha256(session_id.encode()).digest()
    return f"{ADJECTIVES[digest[0] % len(ADJECTIVES)]}-{NOUNS[digest[1] % len(NOUNS)]}"


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_claim(path: Path) -> dict:
    """Read the `key: value` header of a claim file (everything before the first blank line)."""
    claim = {"path": path}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() in FIELDS:
            claim[key.strip()] = value.strip()
    claim.setdefault("name", path.stem)
    return claim


def write_claim(claim: dict) -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = SESSIONS_DIR / f"{claim['name']}.md"
    header = "\n".join(f"{k}: {claim.get(k, '')}" for k in FIELDS)
    path.write_text(
        header + "\n\n<!-- written by scripts/sessions.py; edit via claim/beat, not by hand -->\n",
        encoding="utf-8",
    )
    return path


def load_claims() -> list[dict]:
    if not SESSIONS_DIR.is_dir():
        return []
    return [parse_claim(p) for p in sorted(SESSIONS_DIR.glob("*.md")) if p.name != "README.md"]


def is_stale(claim: dict, at: datetime) -> bool:
    try:
        updated = datetime.fromisoformat(claim.get("updated", ""))
    except ValueError:
        return True
    return at - updated > timedelta(hours=STALE_HOURS)


def scope_list(claim: dict) -> list[str]:
    return [s.strip() for s in claim.get("scope", "").split(",") if s.strip()]


def scopes_overlap(a: list[str], b: list[str]) -> list[str]:
    """Pairs of scope entries that can name the same file (equal, prefix dir, or glob match)."""
    hits = []
    for x in a:
        for y in b:
            xs, ys = x.rstrip("/"), y.rstrip("/")
            if (
                xs == ys
                or xs.startswith(ys + "/")
                or ys.startswith(xs + "/")
                or fnmatch.fnmatch(xs, ys)
                or fnmatch.fnmatch(ys, xs)
            ):
                hits.append(x if x == y else f"{x} ~ {y}")
    return hits


def age(claim: dict, at: datetime) -> str:
    try:
        minutes = int((at - datetime.fromisoformat(claim["updated"])).total_seconds() // 60)
    except (KeyError, ValueError):
        return "?"
    return f"{minutes}m" if minutes < 120 else f"{minutes // 60}h"


def board_lines(claims: list[dict], me: str | None, at: datetime) -> list[str]:
    live = [c for c in claims if not is_stale(c, at)]
    stale = [c for c in claims if is_stale(c, at)]
    lines = []
    if not claims:
        return ["Нет ни одной заявленной сессии (docs/sessions/ пуст)."]
    for c in live:
        tag = " ← ты" if c["name"] == me else ""
        lines.append(
            f"· {c['name']}{tag} [{c.get('lane', '?')}] {age(c, at)} назад — цель: {c.get('goal', '')}"
        )
        if c.get("now"):
            lines.append(f"    сейчас: {c['now']}")
        if c.get("scope"):
            lines.append(f"    файлы: {c['scope']}")
    for c in stale:
        lines.append(
            f"· {c['name']} [{c.get('lane', '?')}] STALE {age(c, at)} — лейн свободен; цель была: {c.get('goal', '')}"
        )
    for i, a in enumerate(live):
        for b in live[i + 1 :]:
            hits = scopes_overlap(scope_list(a), scope_list(b))
            if hits:
                lines.append(f"⚠ ПЕРЕСЕЧЕНИЕ {a['name']} × {b['name']}: {', '.join(hits)}")
    return lines


def resolve_session_id(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    if os.environ.get("CLAUDE_SESSION_ID"):
        return os.environ["CLAUDE_SESSION_ID"]
    return None


def find_own(claims: list[dict], session_id: str) -> dict | None:
    for c in claims:
        if c.get("session_id") == session_id:
            return c
    return None


def cmd_board(args) -> int:
    sid = resolve_session_id(args.session)
    me = name_for(sid) if sid else None
    print("\n".join(board_lines(load_claims(), me, now_utc())))
    return 0


def cmd_claim(args) -> int:
    sid = resolve_session_id(args.session)
    if not sid:
        print("Не знаю id сессии: передай --session (его печатает хук).", file=sys.stderr)
        return 2
    at = now_utc()
    claims = load_claims()
    own = find_own(claims, sid) or {}
    name = own.get("name") or name_for(sid)
    for c in claims:
        if c["name"] == name or c.get("lane") != args.lane:
            continue
        if is_stale(c, at):
            c["path"].unlink()
            print(
                f"Лейн {args.lane}: забрал у протухшей {c['name']} (цель была: {c.get('goal', '')})."
            )
        else:
            print(
                f"⚠ Лейн {args.lane} уже держит живая {c['name']} ({age(c, at)} назад): {c.get('now', '')}. "
                "Договорись через её хендофф или возьми другой лейн.",
                file=sys.stderr,
            )
    claim = {
        "name": name,
        "session_id": sid,
        "lane": args.lane,
        "goal": args.goal,
        "now": args.now or own.get("now", ""),
        "scope": ",".join(args.scope) if args.scope else own.get("scope", ""),
        "branch": args.branch or own.get("branch", ""),
        "started": own.get("started") or at.isoformat(),
        "updated": at.isoformat(),
    }
    path = write_claim(claim)
    print(f"Ты — {name}, лейн {args.lane}. Заявка: {path.relative_to(ROOT)}")
    others = [c for c in load_claims() if c["name"] != name and not is_stale(c, at)]
    for c in others:
        hits = scopes_overlap(scope_list(claim), scope_list(c))
        if hits:
            print(
                f"⚠ ПЕРЕСЕЧЕНИЕ с {c['name']} [{c.get('lane')}]: {', '.join(hits)}", file=sys.stderr
            )
    return 0


def cmd_beat(args) -> int:
    sid = resolve_session_id(args.session)
    own = find_own(load_claims(), sid) if sid else None
    if not own:
        print("У этой сессии нет заявки — сначала `claim`.", file=sys.stderr)
        return 2
    if args.now:
        own["now"] = args.now
    if args.goal:
        own["goal"] = args.goal
    if args.branch:
        own["branch"] = args.branch
    own["updated"] = now_utc().isoformat()
    write_claim(own)
    print(f"{own['name']}: обновлено.")
    return 0


def cmd_done(args) -> int:
    sid = resolve_session_id(args.session)
    own = find_own(load_claims(), sid) if sid else None
    if not own:
        print("Заявки нет — снимать нечего.")
        return 0
    own["path"].unlink()
    print(f"{own['name']}: заявка снята, лейн {own.get('lane')} свободен.")
    return 0


def cmd_hook(args) -> int:
    """SessionStart hook: name the session, show the board, and on compact/resume — its own goal."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    sid = payload.get("session_id") or ""
    source = payload.get("source", "startup")
    if not sid:
        return 0
    name = name_for(sid)
    claims = load_claims()
    own = find_own(claims, sid)
    out = [
        f"[sessions] Ты — сессия «{name}» (id {sid}). Так подписывай хендоффы и сообщения другим сессиям."
    ]
    if own:
        out.append(
            f"Твой лейн: {own.get('lane')} · ЦЕЛЬ: {own.get('goal')} · последний шаг: {own.get('now')}"
        )
        if source in ("compact", "resume"):
            out.append("Контекст сжимался/возобновлялся — сверь, что делаешь, с ЦЕЛЬЮ выше.")
        out.append(f"Обновлять: `python3 scripts/sessions.py beat --session {sid} --now '<шаг>'`.")
    else:
        out.append(
            "Заявки нет. Как только понятна задача: `python3 scripts/sessions.py claim --session "
            f"{sid} --lane <лейн> --goal '<конечный результат одной строкой>' --now '<шаг>' "
            "--scope <файлы,через,запятую>`. Меняется шаг — `beat --now`. Лейн закрыт — `done`."
        )
    out.append("Кто ещё работает:")
    out.extend(board_lines(claims, name, now_utc()))
    print("\n".join(out))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("board")
    p.add_argument("--session")
    p.set_defaults(func=cmd_board)
    p = sub.add_parser("claim")
    p.add_argument("--session")
    p.add_argument("--lane", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--now")
    p.add_argument("--scope", nargs="*")
    p.add_argument("--branch")
    p.set_defaults(func=cmd_claim)
    p = sub.add_parser("beat")
    p.add_argument("--session")
    p.add_argument("--now")
    p.add_argument("--goal")
    p.add_argument("--branch")
    p.set_defaults(func=cmd_beat)
    p = sub.add_parser("done")
    p.add_argument("--session")
    p.set_defaults(func=cmd_done)
    p = sub.add_parser("hook")
    p.set_defaults(func=cmd_hook)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
