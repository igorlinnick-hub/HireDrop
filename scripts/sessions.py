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
    python3 scripts/sessions.py sign                      # first line of the handoff
    python3 scripts/sessions.py done                      # the lane is finished: drop the claim
    python3 scripts/sessions.py stats [--days 7]          # is the board doing its job, in numbers
    python3 scripts/sessions.py hook | hook-prompt | hook-beat | hook-end
                                                          # SessionStart / UserPromptSubmit / Stop / SessionEnd

Being on the board is NOT up to the session (the service-registry rule: a process registers
on start and the runtime keeps its heartbeat; the worker only declares intent). The hooks
put every session on the board the moment it opens (status "open"), take a provisional goal
from its first request, and refresh `updated` on every turn. The session's own job is the
part a hook cannot guess: `claim` with the lane, the real goal and the files. 10-08 a
read-only "count the AI costs" session never claimed and was invisible — that is the hole
this closes.

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
- /clear or exit (SessionEnd hook) PAUSES the claim: the lane keeps its goal, step and handoff,
  and the next session that claims the lane continues it (goal is inherited, not retyped).
- scope overlap with another live claim is reported loudly: that is the "two sessions in one
  content.js" accident CLAUDE.md warns about.
"""

import argparse
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import statistics
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = ROOT / "docs" / "sessions"
STALE_HOURS = 8  # an active claim with no beat this long: the session died without saying so
PAUSED_DAYS = 7  # a lane parked by /clear or exit waits this long for someone to pick it up
BEAT_EVERY_SECS = 60  # the Stop/prompt hooks rewrite the claim at most this often
AUTO_PREFIX = "авто: "  # a goal the hook took from the first request, not one the session declared
FIELDS = (
    "name",
    "session_id",
    "status",
    "lane",
    "goal",
    "now",
    "scope",
    "branch",
    "handoff",
    "started",
    "updated",
    "prompts",
)

# What the standard promises, checked by `stats`. Each target is a loss we already paid for:
# an invisible session (10-08), a lane nobody can see the files of (two sessions in one
# content.js), a lane that ended without a handoff, a claim that died without pausing.
TARGETS = {
    "visible": 1.0,  # sessions that did any work and were on the board
    "claimed": 0.8,  # of sessions with >= CLAIM_EXPECTED_AFTER requests: declared a lane
    "handoff": 1.0,  # of lanes parked or finished: a handoff path was on the claim
    "silent_deaths": 0,  # claimed lanes that went stale without a pause or done
}
CLAIM_EXPECTED_AFTER = 3  # a 1-2 request session (a quick question) is not expected to claim

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


def is_paused(claim: dict) -> bool:
    return claim.get("status") == "paused"


def is_open(claim: dict) -> bool:
    """On the board by the hook, lane not declared yet."""
    return claim.get("status") == "open"


def is_stale(claim: dict, at: datetime) -> bool:
    try:
        updated = datetime.fromisoformat(claim.get("updated", ""))
    except ValueError:
        return True
    limit = timedelta(days=PAUSED_DAYS) if is_paused(claim) else timedelta(hours=STALE_HOURS)
    return at - updated > limit


def is_live(claim: dict, at: datetime) -> bool:
    """A declared lane someone is working on right now."""
    return not is_paused(claim) and not is_open(claim) and not is_stale(claim, at)


def prompts_of(claim: dict) -> int:
    try:
        return int(claim.get("prompts") or 0)
    except ValueError:
        return 0


# ── event log: what `stats` counts ─────────────────────────────────────────────
#
# Local and append-only (gitignored): one JSON line per lifecycle step, written by the hooks
# and the commands themselves, so the numbers fill without anyone remembering to record them.


def events_path() -> Path:
    return SESSIONS_DIR / ".events.jsonl"


def log_event(kind: str, claim: dict, **extra) -> None:
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        row = {
            "at": now_utc().isoformat(),
            "kind": kind,
            "sid": claim.get("session_id", ""),
            "name": claim.get("name", ""),
            **extra,
        }
        with events_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass  # the board must never fail because the log could not be written


def load_events() -> list[dict]:
    try:
        lines = events_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# Claim files are committed with handoffs, so a goal lifted from a request must not carry
# what people paste into requests: keys, tokens, addresses.
_SECRETS = (
    re.compile(r"(?i)\b(token|key|secret|password|passwd|bearer)\b\s*[:=]?\s*\S+"),
    re.compile(r"\b(sk|pk|rk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{8,}"),
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
)


def goal_from_prompt(prompt: str, limit: int = 120) -> str:
    """The first readable line of a request, scrubbed, as a provisional goal ('' if none)."""
    for raw in (prompt or "").splitlines():
        line = raw.strip()
        # Slash commands, pasted blocks, system tags, image placeholders — not the task.
        if not line or line.startswith(("<", "/", "[Image", "```")):
            continue
        for pat in _SECRETS:
            line = pat.sub("…", line)
        line = re.sub(r"\s+", " ", line).strip(" .…")
        if len(line) < 4:
            continue
        return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return ""


def open_claim(session_id: str, at: datetime) -> dict:
    """What the hook writes for a session that has not declared a lane."""
    return {
        "name": name_for(session_id),
        "session_id": session_id,
        "status": "open",
        "lane": "",
        "goal": "",
        "now": "",
        "scope": "",
        "branch": "",
        "handoff": "",
        "started": at.isoformat(),
        "updated": at.isoformat(),
        "prompts": "0",
    }


def drop_dead_open_claims(claims: list[dict], at: datetime) -> list[dict]:
    """An open claim gone quiet holds no lane to free — remove it instead of showing STALE."""
    kept = []
    for c in claims:
        if is_open(c) and is_stale(c, at):
            with contextlib.suppress(OSError):
                c["path"].unlink()
            continue
        kept.append(c)
    return kept


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
    live = [c for c in claims if is_live(c, at)]
    paused = [c for c in claims if is_paused(c) and not is_stale(c, at)]
    stale = [c for c in claims if is_stale(c, at) and not is_open(c)]
    # Opened but never asked anything: an idle tab, not work — off the board.
    undeclared = [c for c in claims if is_open(c) and not is_stale(c, at) and prompts_of(c) > 0]
    lines = []
    if not (live or paused or stale or undeclared):
        return ["Нет ни одной заявленной сессии (docs/sessions/ пуст)."]
    for c in paused:
        lines.append(
            f"· ПАУЗА [{c.get('lane', '?')}] (была {c['name']}, {age(c, at)} назад) — цель: {c.get('goal', '')}"
        )
        lines.append(
            f"    остановилась на: {c.get('now', '')} · хендофф: {c.get('handoff') or '—'}"
            f" · ветка: {c.get('branch') or '—'}"
            f" · продолжить: claim --lane {c.get('lane', '?')}"
        )
    for c in live:
        tag = " ← ты" if c["name"] == me else ""
        lines.append(
            f"· {c['name']}{tag} [{c.get('lane', '?')}] {age(c, at)} назад — цель: {c.get('goal', '')}"
        )
        if c.get("now"):
            lines.append(f"    сейчас: {c['now']}")
        if c.get("scope"):
            lines.append(f"    файлы: {c['scope']}")
        if c.get("handoff"):
            lines.append(f"    хендофф: {c['handoff']}")
        if c.get("branch"):
            lines.append(f"    ветка: {c['branch']}")
    for c in undeclared:
        tag = " ← ты" if c["name"] == me else ""
        goal = c.get("goal") or "—"
        lines.append(
            f"· {c['name']}{tag} [лейн не заявлен] {age(c, at)} назад, запросов {prompts_of(c)} — цель: {goal}"
        )
        lines.append("    файлы не заявлены — пересечения с ней доска не видит")
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


def same_session(a: str, b: str) -> bool:
    """Ids match, or one is a prefix (>= 8 chars) of the other. 10-08 ivory-ibis claimed with
    `--session 158b15fc`; the hooks see the full uuid — without this the session would get a
    second, nameless row on the board."""
    if not a or not b:
        return False
    short, full = sorted((a, b), key=len)
    return short == full or (len(short) >= 8 and full.startswith(short))


def find_own(claims: list[dict], session_id: str) -> dict | None:
    for c in claims:
        if c.get("session_id") == session_id:
            return c
    for c in claims:
        if same_session(c.get("session_id", ""), session_id):
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
    inherited: dict = {}
    for c in claims:
        if c["name"] == name or c.get("lane") != args.lane:
            continue
        if is_paused(c) or is_stale(c, at):
            c["path"].unlink()
            if not is_paused(c):
                log_event("stale_takeover", c, lane=c.get("lane", ""))  # died without a pause
            inherited = inherited or c
            print(
                f"Лейн {args.lane}: продолжаю за {c['name']} — цель: {c.get('goal', '')} · "
                f"остановилась на: {c.get('now', '')} · хендофф: {c.get('handoff') or '—'}"
            )
        else:
            print(
                f"⚠ Лейн {args.lane} уже держит живая {c['name']} ({age(c, at)} назад): {c.get('now', '')}. "
                "Договорись через её хендофф или возьми другой лейн.",
                file=sys.stderr,
            )
    # The hook's open claim carries no lane: a paused lane being continued wins over it.
    prev = inherited if (is_open(own) and inherited) else (own or inherited)
    prev_goal = prev.get("goal") or ""
    goal = args.goal or (None if prev_goal.startswith(AUTO_PREFIX) else prev_goal)
    if not goal:
        print(
            "Нужна --goal: лейн новый, продолжить нечего (цель «авто:» из запроса — не цель, "
            "напиши конечный результат одной строкой).",
            file=sys.stderr,
        )
        return 2
    claim = {
        "name": name,
        "session_id": sid,
        "status": "active",
        "lane": args.lane,
        "goal": goal,
        "now": args.now or prev.get("now", ""),
        "scope": ",".join(args.scope) if args.scope else prev.get("scope", ""),
        "branch": args.branch or prev.get("branch", ""),
        "handoff": args.handoff or prev.get("handoff", ""),
        "started": own.get("started") or at.isoformat(),
        "updated": at.isoformat(),
        "prompts": own.get("prompts") or "0",
    }
    path = write_claim(claim)
    print(f"Ты — {name}, лейн {args.lane}. Заявка: {path.relative_to(ROOT)}")
    others = [c for c in load_claims() if c["name"] != name and is_live(c, at)]
    overlaps = 0
    for c in others:
        hits = scopes_overlap(scope_list(claim), scope_list(c))
        if hits:
            overlaps += 1
            print(
                f"⚠ ПЕРЕСЕЧЕНИЕ с {c['name']} [{c.get('lane')}]: {', '.join(hits)}", file=sys.stderr
            )
    log_event(
        "claim",
        claim,
        lane=args.lane,
        first=not own or is_open(own),
        prompts=prompts_of(claim),
        scope=bool(scope_list(claim)),
        overlaps=overlaps,
        inherited=bool(inherited),
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
    if args.handoff:
        own["handoff"] = args.handoff
    if is_open(own):
        print("Лейн ещё не заявлен — сначала `claim --lane … --goal …`.", file=sys.stderr)
        return 2
    own["status"] = "active"
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
    if not is_open(own):
        log_event(
            "done",
            own,
            lane=own.get("lane", ""),
            handoff=bool(own.get("handoff")),
            prompts=prompts_of(own),
        )
    print(f"{own['name']}: заявка снята, лейн {own.get('lane')} свободен.")
    return 0


def cmd_sign(args) -> int:
    """The line every handoff starts with — who wrote it, which lane, where it is going."""
    sid = resolve_session_id(args.session)
    own = find_own(load_claims(), sid) if sid else None
    if not own or is_open(own):
        print("У этой сессии нет заявки — сначала `claim`.", file=sys.stderr)
        return 2
    print(
        f"Сессия: {own['name']} · лейн {own.get('lane')} · цель: {own.get('goal')} · "
        f"шаг: {own.get('now')} · доска: `python3 scripts/sessions.py board`"
    )
    return 0


def _read_hook_payload() -> dict:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def cmd_hook_end(args) -> int:
    """SessionEnd hook (/clear, exit): park the lane instead of leaving it to look alive."""
    try:
        sid = _read_hook_payload().get("session_id") or ""
        own = find_own(load_claims(), sid) if sid else None
        if own and is_open(own):
            own["path"].unlink()  # never declared a lane: nothing to park
            log_event("close", own, prompts=prompts_of(own))
        elif own:
            own["status"] = "paused"
            own["updated"] = now_utc().isoformat()
            write_claim(own)
            log_event(
                "pause",
                own,
                lane=own.get("lane", ""),
                handoff=bool(own.get("handoff")),
                prompts=prompts_of(own),
            )
    except Exception as e:  # noqa: BLE001 — a hook must never break closing a session
        print(f"[sessions] hook-end skipped: {e}", file=sys.stderr)
    return 0


def _claim_command(sid: str) -> str:
    return (
        f"`python3 scripts/sessions.py claim --session {sid} --lane <лейн> --goal "
        "'<конечный результат одной строкой>' --now '<шаг>' --scope <файлы,через,запятую>`"
    )


def cmd_hook(args) -> int:
    """SessionStart hook: put the session on the board, name it, show the board, and on
    compact/resume — its own goal."""
    payload = _read_hook_payload()
    sid = payload.get("session_id") or ""
    source = payload.get("source", "startup")
    if not sid:
        return 0
    at = now_utc()
    name = name_for(sid)
    claims = drop_dead_open_claims(load_claims(), at)
    own = find_own(claims, sid)
    if not own:
        own = open_claim(sid, at)
        own["path"] = write_claim(own)
        claims.append(own)
        log_event("open", own, source=source)
    out = [
        f"[sessions] Ты — сессия «{name}» (id {sid}), ты уже на доске. "
        "Так подписывай хендоффы и сообщения другим сессиям."
    ]
    if not is_open(own):
        out.append(
            f"Твой лейн: {own.get('lane')} · ЦЕЛЬ: {own.get('goal')} · последний шаг: {own.get('now')}"
        )
        if source in ("compact", "resume"):
            out.append("Контекст сжимался/возобновлялся — сверь, что делаешь, с ЦЕЛЬЮ выше.")
        out.append(f"Обновлять: `python3 scripts/sessions.py beat --session {sid} --now '<шаг>'`.")
    else:
        out.append(
            "Лейн не заявлен: пока доска покажет цель из твоего первого запроса. Как только "
            f"задача ясна — {_claim_command(sid)} (даже если только читаешь и считаешь: лейн "
            "и файлы — это то, что видят другие сессии). Продолжаешь лейн с ПАУЗЫ (после /clear) — "
            f"`claim --session {sid} --lane <лейн>` без --goal: цель, шаг и хендофф перейдут к тебе. "
            "Меняется шаг — `beat --now`. Хендофф — первая строка из `sign`, путь `beat --handoff`. "
            "Лейн закрыт — `done`."
        )
    out.append("Кто ещё работает:")
    out.extend(board_lines(claims, name, at))
    print("\n".join(out))
    return 0


def _touch(own: dict, at: datetime, *, force: bool = False) -> bool:
    """Heartbeat: is it time to rewrite `updated`? (Throttled — every turn would churn the file.)"""
    try:
        last = datetime.fromisoformat(own.get("updated", ""))
    except ValueError:
        return True
    return force or (at - last).total_seconds() >= BEAT_EVERY_SECS


def cmd_hook_prompt(args) -> int:
    """UserPromptSubmit hook: count the request, keep the claim alive, and on the first
    request of an undeclared session — take a provisional goal from it and say so once.

    Also the safety net for sessions the start hook missed (opened before it was installed,
    or the hook timed out): the first request registers them."""
    try:
        payload = _read_hook_payload()
        sid = payload.get("session_id") or ""
        if not sid:
            return 0
        at = now_utc()
        own = find_own(load_claims(), sid)
        if not own:
            own = open_claim(sid, at)
            log_event("open", own, source="prompt")  # the start hook did not register it
        n = prompts_of(own) + 1
        own["prompts"] = str(n)
        own["updated"] = at.isoformat()
        if is_paused(own):
            own["status"] = "active"  # resumed after a /clear-less exit: it is working again
        said = []
        if is_open(own) and not own.get("goal"):
            goal = goal_from_prompt(payload.get("prompt") or "")
            if goal:
                own["goal"] = AUTO_PREFIX + goal
                said.append(
                    f"[sessions] На доске ты «{own['name']}», цель пока взята из запроса: «{goal}». "
                    f"Заяви лейн, цель и файлы, когда задача станет ясна: {_claim_command(sid)}."
                )
        elif is_open(own) and n == CLAIM_EXPECTED_AFTER:
            said.append(
                f"[sessions] Уже {n} запроса, а лейн не заявлен — другие сессии не видят твоих файлов. "
                f"{_claim_command(sid)}"
            )
        write_claim(own)
        if n == 1:
            log_event("prompt1", own)
        if said:
            print("\n".join(said))
    except Exception as e:  # noqa: BLE001 — a hook must never block a request
        print(f"[sessions] hook-prompt skipped: {e}", file=sys.stderr)
    return 0


def cmd_hook_beat(args) -> int:
    """Stop hook: the turn ended, the session is alive — refresh `updated` (throttled)."""
    try:
        sid = _read_hook_payload().get("session_id") or ""
        own = find_own(load_claims(), sid) if sid else None
        at = now_utc()
        if own and _touch(own, at):
            own["updated"] = at.isoformat()
            if is_paused(own):
                own["status"] = "active"
            write_claim(own)
    except Exception as e:  # noqa: BLE001
        print(f"[sessions] hook-beat skipped: {e}", file=sys.stderr)
    return 0


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"


def _mark(ok: bool | None) -> str:
    return "" if ok is None else (" ✅" if ok else " ❌")


def stats_lines(events: list[dict], claims: list[dict], at: datetime, days: int) -> list[str]:
    """The standard's targets against what the hooks recorded. Pure — tests feed it events."""
    since = at - timedelta(days=days)

    def when(e: dict) -> datetime | None:
        try:
            return datetime.fromisoformat(e.get("at", ""))
        except ValueError:
            return None

    window = [e for e in events if (when(e) or since) >= since]
    if not window:
        return [
            f"Доска сессий за {days} дн.: данных нет. Лог ({events_path().name}) наполняют хуки "
            "сами — первые цифры после первых сессий с подключёнными хуками."
        ]
    first_at = min(when(e) for e in window if when(e))
    by_sid: dict[str, list[dict]] = {}
    for e in window:
        by_sid.setdefault(e.get("sid", ""), []).append(e)

    # Requests per session: the largest count any event or the live claim file recorded.
    prompts: dict[str, int] = {}
    for e in window:
        if isinstance(e.get("prompts"), int):
            prompts[e["sid"]] = max(prompts.get(e["sid"], 0), e["prompts"])
    for c in claims:
        sid = c.get("session_id", "")
        if sid in by_sid:
            prompts[sid] = max(prompts.get(sid, 0), prompts_of(c))

    worked = [sid for sid, es in by_sid.items() if any(e["kind"] == "prompt1" for e in es)]
    late = [
        sid
        for sid in worked
        if any(e["kind"] == "open" and e.get("source") == "prompt" for e in by_sid[sid])
    ]
    expected = [sid for sid in worked if prompts.get(sid, 0) >= CLAIM_EXPECTED_AFTER]
    first_claims = {
        e["sid"]: e for e in window if e["kind"] == "claim" and e.get("first")
    }  # last write wins; one per session in practice
    claimed = [sid for sid in expected if sid in first_claims]

    def opened_at(sid: str) -> datetime | None:
        opens = [when(e) for e in by_sid[sid] if e["kind"] == "open" and when(e)]
        return min(opens) if opens else None

    claim_prompts = [first_claims[s].get("prompts", 0) for s in claimed]
    claim_minutes = []
    for s in claimed:
        o, c = opened_at(s), when(first_claims[s])
        if o and c:
            claim_minutes.append((c - o).total_seconds() / 60)

    endings = [e for e in window if e["kind"] in ("pause", "done")]
    with_handoff = [e for e in endings if e.get("handoff")]
    deaths = [e for e in window if e["kind"] == "stale_takeover"]
    dead_now = [c for c in claims if not is_open(c) and not is_paused(c) and is_stale(c, at)]
    overlaps = sum(int(e.get("overlaps") or 0) for e in window if e["kind"] == "claim")

    lines = [
        f"Доска сессий за {days} дн. (лог с {first_at:%m-%d %H:%M}Z, "
        f"сессий в логе {len(by_sid)}, работали {len(worked)}):"
    ]
    lines.append(
        f"· Видимость: {len(worked)} из {len(worked)} работавших были на доске "
        f"(цель {TARGETS['visible']:.0%}, обеспечивает хук, а не память сессии)"
        + (f" · {len(late)} встали только по первому запросу — проверь хук старта" if late else "")
    )
    claim_ok = (len(claimed) / len(expected) >= TARGETS["claimed"]) if expected else None
    speed = ""
    if claim_prompts:
        speed = f" · медиана: на {statistics.median(claim_prompts):g}-м запросе"
        if claim_minutes:
            speed += f", через {statistics.median(claim_minutes):.0f} мин"
    lines.append(
        f"· Лейн заявлен: {len(claimed)} из {len(expected)} сессий с ≥{CLAIM_EXPECTED_AFTER} "
        f"запросами ({_pct(len(claimed), len(expected))}, цель ≥{TARGETS['claimed']:.0%})"
        + _mark(claim_ok)
        + speed
    )
    handoff_ok = (len(with_handoff) / len(endings) >= TARGETS["handoff"]) if endings else None
    lines.append(
        f"· Хендофф при закрытии лейна: {len(with_handoff)} из {len(endings)} "
        f"({_pct(len(with_handoff), len(endings))}, цель {TARGETS['handoff']:.0%})"
        + _mark(handoff_ok)
    )
    n_dead = len(deaths) + len(dead_now)
    lines.append(
        f"· Тихие смерти (лейн протух без паузы): {n_dead} (цель {TARGETS['silent_deaths']})"
        + _mark(n_dead <= TARGETS["silent_deaths"])
        + (f" — сейчас висят: {', '.join(c['name'] for c in dead_now)}" if dead_now else "")
    )
    lines.append(f"· Пересечения файлов при заявке: {overlaps} (сигнал, не цель)")
    return lines


def cmd_stats(args) -> int:
    print("\n".join(stats_lines(load_events(), load_claims(), now_utc(), args.days)))
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
    p.add_argument("--goal")
    p.add_argument("--now")
    p.add_argument("--scope", nargs="*")
    p.add_argument("--branch")
    p.add_argument("--handoff")
    p.set_defaults(func=cmd_claim)
    p = sub.add_parser("beat")
    p.add_argument("--session")
    p.add_argument("--now")
    p.add_argument("--goal")
    p.add_argument("--branch")
    p.add_argument("--handoff")
    p.set_defaults(func=cmd_beat)
    p = sub.add_parser("done")
    p.add_argument("--session")
    p.set_defaults(func=cmd_done)
    p = sub.add_parser("sign")
    p.add_argument("--session")
    p.set_defaults(func=cmd_sign)
    p = sub.add_parser("stats")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(func=cmd_stats)
    p = sub.add_parser("hook")
    p.set_defaults(func=cmd_hook)
    p = sub.add_parser("hook-prompt")
    p.set_defaults(func=cmd_hook_prompt)
    p = sub.add_parser("hook-beat")
    p.set_defaults(func=cmd_hook_beat)
    p = sub.add_parser("hook-end")
    p.set_defaults(func=cmd_hook_end)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
