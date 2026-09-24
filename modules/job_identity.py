"""One posting = one identity, however the URL was written.

The pool keys rows on the exact `link` string (`upsert on_conflict=user_id,link`), and the
boards hand us the same posting under several spellings. Live proof from Igor's pool
(2026-09-08), one Braze role as TWO rows:

    boards.greenhouse.io/braze/jobs/8095311?gh_jid=8095311   -> status "approved"
    job-boards.greenhouse.io/braze/jobs/8095311              -> status "applied_unconfirmed"

The apply wrote a NEW row; the swiped one stayed `approved` forever. Three costs, in
rising order of badness: the pool bloats, the queue re-offers a job already done, and on a
fresh browser profile (where the extension's local appliedUrls set is empty) we would
APPLY TWICE — the employer sees it.

So identity is the board's own posting id, not the URL. Everything else — host variant,
query string, /apply and /application suffixes, trailing slash — is spelling.
"""

import re
from urllib.parse import parse_qs, urlparse

# Path tails the boards append to the same posting: an apply page is not another job.
_APPLY_TAILS = {"apply", "application", "applications", "apply-now"}

# A token this short is not an id — matching on it would sweep in unrelated rows.
_MIN_TOKEN = 6

# Query params that are KNOWN tracking noise — the only ones safe to strip. Anything not
# listed is kept: boards we don't know may carry the posting id in a query param (Taleo
# ?job=, ADP ?jobId=, ZipRecruiter ?jid=), and dropping it would collapse different
# postings into one key — the exact wrong-row heal this module exists to prevent.
_TRACKING_PARAMS = {
    "from",
    "ref",
    "referer",
    "referrer",
    "source",
    "src",
    "trk",
    "gclid",
    "fbclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "gh_src",
    "lever-origin",
    "lever-source",
}


def _is_tracking_param(key: str) -> bool:
    k = key.lower()
    return k in _TRACKING_PARAMS or k.startswith("utm_")


def job_identity(url: str) -> str | None:
    """The board's posting id for `url`, or None when the URL carries no stable id.

    None is a real answer: better no dedup than dedup on a shared prefix.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
    except Exception:  # noqa: BLE001 — a malformed URL simply has no identity
        return None

    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query or "")

    # Indeed's identity IS the query: /viewjob?jk=<id>, and the apply flow carries the same
    # jk on smartapply. Path-based dedup would collapse every Indeed job into one.
    if "indeed.com" in host:
        jk = (query.get("jk") or query.get("jobKey") or [""])[0]
        return f"indeed:{jk.lower()}" if len(jk) >= _MIN_TOKEN else None

    # Greenhouse ships the id twice (path + ?gh_jid) across two hosts (boards. and
    # job-boards.) — the exact pair that split Igor's Braze row.
    if "greenhouse.io" in host:
        gh = (query.get("gh_jid") or [""])[0]
        if gh.isdigit() and len(gh) >= _MIN_TOKEN:
            return f"greenhouse:{gh}"

    segments = [s for s in (parsed.path or "").split("/") if s and s.lower() not in _APPLY_TAILS]
    if not segments:
        return None
    tail = segments[-1].lower()
    if not _looks_like_a_posting_id(tail):
        return None

    board = "greenhouse" if "greenhouse.io" in host else host.replace("www.", "")
    return f"{board}:{tail}"


def _looks_like_a_posting_id(tail: str) -> bool:
    """Board ids are numbers or UUID-ish hex — never words.

    The bar has to be this explicit: a bare length+hex check accepts "careers" (c, a, e
    are hex digits) and would then mark every row under /careers applied.
    """
    if len(tail) < _MIN_TOKEN:
        return False
    if tail.isdigit():
        return True
    return bool(re.fullmatch(r"[0-9a-f]{8,}(?:-[0-9a-f]{4,}){0,4}", tail))


def normalized_link(url: str) -> str | None:
    """Canonical spelling of a job URL — the fallback key when the board exposes no
    posting id. Lowercases the host, drops www., strips apply tails and the trailing
    slash, and removes ONLY known tracking noise: unknown query params (sorted) and
    route-like fragments stay in the key, because on unknown boards they may BE the
    posting id. Only EXACT equality of two normalized links may count as a match:
    anything fuzzier (title similarity, prefix match) would mark unrelated rows
    applied, which is worse than missing a twin.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
    except Exception:  # noqa: BLE001 — a malformed URL has no canonical spelling
        return None
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    segments = [s for s in (parsed.path or "").split("/") if s and s.lower() not in _APPLY_TAILS]
    query = parse_qs(parsed.query or "")
    kept = sorted((k, v[0]) for k, v in query.items() if v and v[0] and not _is_tracking_param(k))
    out = host
    if segments:
        out += "/" + "/".join(segments)
    if kept:
        out += "?" + "&".join(f"{k}={v}" for k, v in kept)
    # A bare word anchor (#top, #main-content) is page furniture; anything with more
    # structure (hash routing like #/jobs/123) can name the posting — keep it.
    fragment = (parsed.fragment or "").strip()
    if fragment and not re.fullmatch(r"[A-Za-z][A-Za-z_-]*", fragment):
        out += "#" + fragment
    return out


def same_posting(a: str, b: str) -> bool:
    """True when two URLs name the same posting. False whenever identity is unknown —
    an unknown must never be treated as a match."""
    ia = job_identity(a)
    return bool(ia) and ia == job_identity(b)
