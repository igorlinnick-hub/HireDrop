"""Code standards checks that ruff does not cover (see "Code standards" in CLAUDE.md).

Counts, per tracked .py/.js file:
  history      comments that carry history: a date, a PR/issue number, a name,
               non-English text. History belongs in the commit and the PR.
  empty_catch  `catch {}` with nothing in it (JS; Python is ruff S110).

Files that predate the rules keep their counts in standards-baseline.json. A file
fails when its count goes UP; cleaning one up lowers the bar for everyone after.

    python scripts/standards_ratchet.py            # check (CI)
    python scripts/standards_ratchet.py --update   # rewrite the baseline
"""

from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "standards-baseline.json"

MARKERS = [
    ("a date", re.compile(r"\b(?:20\d\d-)?(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b")),
    (
        "a PR or issue number",
        re.compile(
            r"(?:\b(?:PR|web|HireDrop|jobflow|backend|ext|site|issue)\s*#\d+|\(#\d+\))", re.I
        ),
    ),
    ("a person's name", re.compile(r"\bIgor\b", re.I)),
    ("non-English text", re.compile(r"[А-Яа-яЁё]")),
]
JS_COMMENT_LINE = re.compile(r"^\s*(?://|/\*|\*)")
JS_EMPTY_CATCH = re.compile(r"catch\s*(?:\([^)]*\))?\s*\{\s*\}")


def history_marker(text: str) -> str | None:
    return next((what for what, rx in MARKERS if rx.search(text)), None)


def python_comments(src: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                out.append((tok.start[0], tok.string))
        tree = ast.parse(src)
    except (SyntaxError, tokenize.TokenError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc and node.body:
                start = node.body[0].lineno
                out.extend((start + i, line) for i, line in enumerate(doc.splitlines()))
    return out


def js_comments(src: str) -> list[tuple[int, str]]:
    # Whole-line comments only: a "//" inside a string (every URL) is not a comment.
    return [(n, line) for n, line in enumerate(src.splitlines(), 1) if JS_COMMENT_LINE.match(line)]


def scan(path: Path) -> dict[str, list[str]]:
    src = path.read_text(encoding="utf-8", errors="replace")
    comments = python_comments(src) if path.suffix == ".py" else js_comments(src)
    hits: dict[str, list[str]] = {"history": [], "empty_catch": []}
    for line_no, text in comments:
        what = history_marker(text)
        if what:
            hits["history"].append(f"{line_no}: {what}: {text.strip()[:100]}")
    if path.suffix == ".js":
        for m in JS_EMPTY_CATCH.finditer(src):
            line_no = src.count("\n", 0, m.start()) + 1
            hits["empty_catch"].append(f"{line_no}: {m.group(0)}")
    return hits


def tracked_files() -> list[Path]:
    names = subprocess.run(
        ["git", "ls-files", "*.py", "*.js"],  # noqa: S607 — git from PATH, fixed arguments
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [ROOT / n for n in names if "node_modules/" not in n]


def main() -> int:
    current = {}
    details = {}
    for path in tracked_files():
        hits = scan(path)
        counts = {k: len(v) for k, v in hits.items() if v}
        if counts:
            rel = path.relative_to(ROOT).as_posix()
            current[rel] = counts
            details[rel] = hits

    if "--update" in sys.argv:
        BASELINE.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
        print(f"baseline: {sum(sum(c.values()) for c in current.values())} in {len(current)} files")
        return 0

    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    worse, better = [], 0
    for rel, counts in sorted(current.items()):
        for kind, n in counts.items():
            allowed = baseline.get(rel, {}).get(kind, 0)
            if n > allowed:
                worse.append((rel, kind, n, allowed))
    for rel, counts in baseline.items():
        for kind, allowed in counts.items():
            better += max(0, allowed - current.get(rel, {}).get(kind, 0))

    for rel, kind, n, allowed in worse:
        print(f"{rel}: {kind} {allowed} -> {n}")
        for hit in details[rel][kind]:
            print(f"    {hit}")
    if worse:
        print("\nNew violations of the code standards (CLAUDE.md). Fix the lines you added.")
        return 1
    if better:
        print(f"{better} fewer than the baseline: run with --update to lock it in.")
    print("standards ratchet: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
