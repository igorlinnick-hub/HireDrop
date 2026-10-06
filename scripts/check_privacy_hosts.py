#!/usr/bin/env python3
"""Does the live privacy policy name every site the extension can run on?

The manifest and the policy live in different repos and nobody compared them. By 10-06
the policy still said the extension "runs only on indeed.com" while the manifest covered
ZipRecruiter, Greenhouse, Lever, Ashby and an optional <all_urls> pill. A Chrome Web Store
reviewer compares exactly these two things, and the user reads the policy as the truth.

So every host the manifest grants (host_permissions, content_scripts.matches,
optional_host_permissions) must be named on https://hiredrop.io/privacy. A new platform in
the manifest turns CI red until the policy says it — policy first, then the manifest.

    python scripts/check_privacy_hosts.py                 # live page
    python scripts/check_privacy_hosts.py --file page.html
    python scripts/check_privacy_hosts.py --url https://preview.vercel.app/privacy

Exit 0 = every host is named; 1 = some are missing (listed); 2 = page could not be read.
Stdlib only.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "chrome-extension" / "manifest.json"
POLICY_URL = "https://hiredrop.io/privacy"

# Hosts that are our own infrastructure, not sites the user visits: the policy names them
# by role. Keyed by registrable domain.
ALIASES = {
    "railway.app": "HireDrop backend",
    "supabase.co": "Supabase",
}
# A pattern that matches every site needs the policy to say what happens on other websites.
ALL_SITES = {"<all_urls>", "*://*/*", "https://*/*", "http://*/*"}
ALL_SITES_PHRASE = "other websites"


def manifest_patterns(manifest: dict) -> list[str]:
    pats: list[str] = []
    pats += manifest.get("host_permissions", [])
    pats += manifest.get("optional_host_permissions", [])
    for cs in manifest.get("content_scripts", []):
        pats += cs.get("matches", [])
    return sorted(set(pats))


def required_phrase(pattern: str) -> str:
    """The text the policy must contain for this match pattern."""
    if pattern in ALL_SITES:
        return ALL_SITES_PHRASE
    host = re.sub(r"^[a-z*]+://", "", pattern).split("/", 1)[0].lstrip("*.")
    domain = ".".join(host.split(".")[-2:])
    return ALIASES.get(domain, domain)


def page_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).lower()


def fetch(url: str, tries: int = 3) -> str:
    if not url.startswith("https://"):
        raise ValueError("https only")
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hiredrop-ci-privacy-check"})  # noqa: S310
            with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310  # nosec B310
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # network blips: retry, then report
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"could not read {url}: {last}")


def missing(manifest: dict, text: str) -> list[tuple[str, str]]:
    out = []
    for p in manifest_patterns(manifest):
        phrase = required_phrase(p)
        if phrase.lower() not in text:
            out.append((p, phrase))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=POLICY_URL)
    ap.add_argument("--file", help="read the policy HTML from a file instead of the network")
    args = ap.parse_args(argv)

    manifest = json.loads(MANIFEST.read_text())
    try:
        raw = Path(args.file).read_text() if args.file else fetch(args.url)
    except Exception as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    gaps = missing(manifest, page_text(raw))
    src = args.file or args.url
    if gaps:
        print(f"✗ {src} does not name {len(gaps)} host(s) the extension's manifest grants:")
        for p, phrase in gaps:
            print(f"   {p:45} needs “{phrase}”")
        print(
            "  Fix the policy first (jobflow-website app/privacy/page.tsx), then ship the manifest."
        )
        return 1
    print(f"✓ {src} names all {len(manifest_patterns(manifest))} manifest host patterns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
