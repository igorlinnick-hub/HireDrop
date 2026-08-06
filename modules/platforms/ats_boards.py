"""Direct-source discovery from ATS public board APIs (Greenhouse + Lever).

Why this exists (ROADMAP_E2E.md): most GOOD-FIT jobs are external-apply on Greenhouse/
Lever, and boards (ZR/Indeed) hide the destination URL behind a redirect. But Greenhouse
and Lever each expose a PUBLIC, official JSON board API that returns direct apply URLs in
the exact shape `phase_ats` already fills — no scraping, no anti-bot risk, server-side.

There is NO global keyword search across all companies — you query per-company — so this
module takes a curated list of company tokens (a "watchlist") and pulls their live jobs.

Verified live 2026-07-12:
  Greenhouse: GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs
              -> job['absolute_url'] = https://job-boards.greenhouse.io/{token}/jobs/{id}
  Lever:      GET https://api.lever.co/v0/postings/{token}?mode=json
              -> posting['applyUrl'] = https://jobs.lever.co/{token}/{uuid}/apply
"""
from __future__ import annotations

import html as _html
import re as _re

import requests

# `?content=true` makes the Greenhouse board API include each job's full description
# (HTML) in the LIST response — one request, all descriptions. Without it, GH jobs had
# EMPTY descriptions, so the fit-scorer saw only the title and scored them near-zero
# (~6% in the swipe deck). Lever's postings API already returns descriptionPlain.
GREENHOUSE_BOARD_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
LEVER_POSTINGS_API = "https://api.lever.co/v0/postings/{token}?mode=json"
# Ashby public job-board API — returns jobs with descriptionPlain + jobUrl/applyUrl
# (both jobs.ashbyhq.com/<org>/<id>). Guest-apply, form at <jobUrl>/application.
ASHBY_BOARD_API = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
# Workday public "cxs" discovery API — POST per tenant/site (there's no global search).
# tenant + data-center (wd1..wd12) + site are company-specific → the watchlist token encodes
# all three as "tenant|dc|site". DISCOVERY ONLY for now: Workday apply is an account-gated
# multi-step wizard (My Info → Experience → Questions → Voluntary → Review), NOT the single-
# page form phase_ats fills, so these jobs feed the pool ("scrapeable") but the executor
# never walks them (workday ∉ ATS_PLATFORMS). Verified live 2026-07-31: 2.8k marketing roles
# across nvidia/adobe/salesforce/mastercard/hp/paypal/target/workday/cvshealth.
WORKDAY_CXS_API = "https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"

_TAG_RE = _re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    """GH `content` is HTML-escaped markup; turn it into plain text for scoring."""
    if not s:
        return ""
    return _re.sub(r"\s+", " ", _TAG_RE.sub(" ", _html.unescape(s))).strip()

_UA = {"User-Agent": "HireDrop/1.0 (+https://hiredrop.io)"}
# Per-board HTTP timeout (connect + read). Kept tight: a single slow/hanging board or a
# Cloudflare challenge must not stall discovery. `discover_ats` fetches boards in PARALLEL
# and abandons stragglers past _DISCOVER_DEADLINE, so total discovery time is bounded no
# matter how many boards are slow (P0: sequential fetch = 46×timeout held a worker for
# minutes → API 000).
_TIMEOUT = 6
_DISCOVER_DEADLINE = 35  # hard cap (seconds) on a whole discovery sweep. Bumped 25→35 as the
# watchlist grew to ~196 boards (find-ats runs in a background thread since #64, so a longer
# sweep no longer holds an API worker — the deadline just bounds total work).
_DISCOVER_WORKERS = 16

# Hosts phase_ats can actually fill (detectPlatform recognizes greenhouse.io / lever.co).
# Some companies (stripe, databricks, coinbase…) embed Greenhouse but expose the job on
# their OWN career domain (stripe.com/jobs?gh_jid=…) — the board API still lists those, but
# their apply_url isn't a form phase_ats handles, so we drop them here rather than hand the
# filler a URL it can't drive. (Verified 2026-07-12: ~half of GH matches are custom-domain.)
_FILLABLE_SUFFIXES = ("greenhouse.io", "lever.co", "ashbyhq.com")


def _is_fillable(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        h = urlparse(url).hostname or ""
        return any(h == d or h.endswith("." + d) for d in _FILLABLE_SUFFIXES)
    except Exception:
        return False


# Field-agnostic role modifiers (seniority/type/connectors). We DON'T require these to
# match — "Manager" / "Senior" / "Lead" appear in almost every title, so requiring the
# whole phrase "social media manager" matched ~2 of 5000 postings (the "pool = 48 ever"
# yield bug). Dropping these leaves the DISTINCTIVE field words (marketing, social, media,
# healthcare, engineer, sales…) which is what the user actually cares about. Recall comes
# from these words; precision comes from the AI fit-scorer, which ranks the deck best-first.
_ROLE_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "for", "to", "in", "at", "with", "on", "by", "new",
    "manager", "lead", "senior", "junior", "associate", "director", "coordinator", "specialist",
    "analyst", "staff", "principal", "head", "officer", "intern", "contract", "remote", "ii",
    "iii", "sr", "jr", "vp", "representative", "assistant", "team", "global", "manager,",
}


def _distinctive_words(keyword: str) -> list[str]:
    return [w for w in _re.split(r"\W+", keyword.lower()) if len(w) >= 3 and w not in _ROLE_STOPWORDS]


def _keyword_match(text: str, keywords: list[str] | None) -> bool:
    """No keywords -> match everything. Otherwise match on either the FULL keyword phrase
    OR any DISTINCTIVE word of a keyword (generic role words like "manager" are ignored,
    so "social media manager" matches "Social Media Coordinator" etc.). Measured live:
    lifts Igor's yield from 2 → ~207 relevant postings across the 46-board watchlist."""
    if not keywords:
        return True
    t = (text or "").lower()
    for k in keywords:
        k = k.strip().lower()
        if not k:
            continue
        if k in t:
            return True
        words = _distinctive_words(k)
        if words and any(w in t for w in words):
            return True
    return False


# Captcha burden per platform now lives in the single source modules/captcha_profile.py
# (shared with app/routers/jobs.py so the dashboard/campaign see the same touch labels).
# Server-side static HTML can't tell v2-checkbox from v3-invisible (widget is client-
# rendered), so we rank by the reliable PLATFORM signal, not per-company HTML guessing.
from modules.captcha_profile import TOUCH_RANK as _TOUCH_RANK
from modules.captcha_profile import captcha_touch as _captcha_touch_pt


def _captcha_touch(token: str, platform: str) -> str:
    # ats_boards passes (token, platform); the shared helper takes (platform, token).
    return _captcha_touch_pt(platform, token)


def _job(title, company, apply_url, location, platform, description=""):
    touch = _captcha_touch(company, platform)
    return {
        "title": (title or "").strip(),
        "company": company,
        "link": apply_url,          # the direct apply URL — what phase_ats navigates to
        "apply_url": apply_url,
        "location": (location or "").strip(),
        "platform": platform,       # "greenhouse" | "lever" (matches detectPlatform)
        "description": (description or "")[:1500],
        "source": "ats_board",
        "captcha_touch": touch,        # "low" | "medium" | "high" — expected human-solve burden
        "zero_touch": touch == "low",  # true = usually submits with no captcha action needed
    }


def fetch_greenhouse(token: str, keywords: list[str] | None = None, limit: int = 50) -> list[dict]:
    """Live Greenhouse jobs for one company board. Returns [] on any error (404 = bad token)."""
    try:
        r = requests.get(GREENHOUSE_BOARD_API.format(token=token), headers=_UA, timeout=_TIMEOUT)
        if r.status_code != 200:
            return []
        jobs = r.json().get("jobs", []) or []
    except Exception:
        return []
    out = []
    for j in jobs:
        title = j.get("title", "")
        loc = (j.get("location") or {}).get("name", "")
        if not _keyword_match(f"{title} {loc}", keywords):
            continue
        url = j.get("absolute_url")
        if not url or not _is_fillable(url):
            continue  # custom career-domain embeds (stripe.com/jobs…) aren't phase_ats-fillable
        # Real description (from ?content=true) so the fit-scorer ranks GH jobs properly.
        out.append(_job(title, token, url, loc, "greenhouse", _strip_html(j.get("content", ""))))
        if len(out) >= limit:
            break
    return out


def fetch_lever(token: str, keywords: list[str] | None = None, limit: int = 50) -> list[dict]:
    """Live Lever postings for one company. Returns [] on any error / non-Lever token."""
    try:
        r = requests.get(LEVER_POSTINGS_API.format(token=token), headers=_UA, timeout=_TIMEOUT)
        if r.status_code != 200:
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
    except Exception:
        return []
    out = []
    for p in data:
        title = p.get("text", "")
        loc = ((p.get("categories") or {}).get("location")) or ""
        if not _keyword_match(f"{title} {loc}", keywords):
            continue
        # applyUrl is the /apply form (what phase_ats fills); hostedUrl is the JD page.
        url = p.get("applyUrl") or (p.get("hostedUrl", "") + "/apply" if p.get("hostedUrl") else None)
        if not url or not _is_fillable(url):
            continue
        lever_desc = p.get("descriptionPlain") or _strip_html(p.get("description", ""))
        out.append(_job(title, token, url, loc, "lever", lever_desc))
        if len(out) >= limit:
            break
    return out


def fetch_ashby(token: str, keywords: list[str] | None = None, limit: int = 50) -> list[dict]:
    """Live Ashby postings for one company. Returns [] on any error / bad token.

    Ashby's board API returns descriptionPlain (no HTML strip needed) and jobUrl/applyUrl
    at jobs.ashbyhq.com/<org>/<id>. The guest application form is at <jobUrl>/application,
    so we target that directly (phase_ats lands on the form, not the JD page)."""
    try:
        r = requests.get(ASHBY_BOARD_API.format(token=token), headers=_UA, timeout=_TIMEOUT)
        if r.status_code != 200:
            return []
        jobs = (r.json() or {}).get("jobs", []) or []
    except Exception:
        return []
    out = []
    for j in jobs:
        if j.get("isListed") is False:
            continue
        title = (j.get("title") or "").strip()
        loc = j.get("location") or ""
        if not _keyword_match(f"{title} {loc}", keywords):
            continue
        base = (j.get("applyUrl") or j.get("jobUrl") or "").rstrip("/")
        if not base or not _is_fillable(base):
            continue
        # Ashby's applyUrl ALREADY ends in /application; jobUrl does not. Append once,
        # idempotently — the old unconditional append produced /application/application,
        # a stub page with NO form fields -> phase_ats "couldn't open" -> 0 Ashby submits
        # (2026-08-04 root-cause). Land on the real form either way.
        url = base if base.endswith("/application") else base + "/application"
        desc = j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml", ""))
        out.append(_job(title, token, url, loc, "ashby", desc))
        if len(out) >= limit:
            break
    return out


def fetch_workday(token: str, keywords: list[str] | None = None, limit: int = 50) -> list[dict]:
    """Live Workday postings for one tenant. token = "tenant|dc|site".

    DISCOVERY ONLY — Workday apply is an account-gated multi-step wizard (not phase_ats-
    fillable), so we do NOT run the _is_fillable gate here; these jobs enter the pool for
    "scrapeable" coverage but the extension never walks them (workday ∉ ATS_PLATFORMS).
    The cxs API is a POST with a searchText query (there is no global search)."""
    try:
        tenant, dc, site = token.split("|")
    except ValueError:
        return []
    url = WORKDAY_CXS_API.format(tenant=tenant, dc=dc, site=site)
    hdr = {**_UA, "Content-Type": "application/json", "Accept": "application/json"}
    # Workday's searchText AND-matches EVERY word, so joining several keyword PHRASES into one
    # query returns nothing (live 2026-07-31: "healthcare marketing social media manager" → 0,
    # but "healthcare marketing" → 29 and "social media manager" → 13). Query each keyword
    # phrase separately and merge, deduping by externalPath. searchText="" (no keywords) just
    # returns the most-recent jobs, which _keyword_match then can't narrow — so keep it as a
    # last resort only.
    queries = [k for k in (keywords or []) if k and k.strip()] or [""]
    posts: dict[str, dict] = {}
    for q in queries[:4]:
        try:
            r = requests.post(
                url, headers=hdr,
                json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q},
                timeout=_TIMEOUT,
            )
            if r.status_code != 200:
                continue
            for p in (r.json() or {}).get("jobPostings", []) or []:
                path = p.get("externalPath")
                if path and path not in posts:
                    posts[path] = p
        except Exception:
            continue
    base = f"https://{tenant}.{dc}.myworkdayjobs.com/{site}"
    out = []
    for path, p in posts.items():
        title = (p.get("title") or "").strip()
        loc = p.get("locationsText") or ""
        if not _keyword_match(f"{title} {loc}", keywords):
            continue
        # No description in the list response (a per-job detail call is a separate endpoint);
        # the thin-description backfill / title-based scoring handles it (min_score=0 keeps them).
        out.append(_job(title, tenant, base + path, loc, "workday", ""))
        if len(out) >= limit:
            break
    return out


_FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
}


def discover_ats(companies: list[tuple[str, str]], keywords: list[str] | None = None,
                 cap: int = 100) -> list[dict]:
    """Pull live jobs across a watchlist of (token, platform) companies, keyword-filtered.

    Boards are fetched in PARALLEL with a hard overall deadline (_DISCOVER_DEADLINE): one
    slow board or a Cloudflare challenge can no longer hold the caller for minutes (the
    old sequential loop was the API-000 worker-starvation cause). Dedups by apply_url and
    keeps zero-touch (low-captcha) destinations first so the cap fills with the easy wins.
    """
    import concurrent.futures
    from collections import deque

    fetch_targets = [(t, p) for t, p in companies if p in _FETCHERS]
    # Interleave by platform so a platform listed LAST in the watchlist (e.g. Workday, 9
    # boards after 187 others) isn't starved when _DISCOVER_DEADLINE cuts off stragglers:
    # spreading each platform's boards through the submission order makes the deadline trim
    # all platforms proportionally instead of killing whichever comes last (live 2026-07-31:
    # Workday landed only 3 of ~40 because its boards were submitted after GH/Lever/Ashby).
    _byp: dict[str, deque] = {}
    for t, p in fetch_targets:
        _byp.setdefault(p, deque()).append((t, p))
    _queues = list(_byp.values())
    _interleaved: list[tuple[str, str]] = []
    while any(_queues):
        for q in _queues:
            if q:
                _interleaved.append(q.popleft())
    fetch_targets = _interleaved

    def _one(token: str, platform: str) -> list[dict]:
        try:
            return _FETCHERS[platform](token, keywords) or []
        except Exception:
            return []

    collected: list[dict] = []
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=_DISCOVER_WORKERS)
    try:
        futs = [ex.submit(_one, t, p) for t, p in fetch_targets]
        done, _pending = concurrent.futures.wait(futs, timeout=_DISCOVER_DEADLINE)
        for fut in done:
            try:
                collected.extend(fut.result())
            except Exception:
                pass
    finally:
        # Never block on stragglers — their per-board requests time out at _TIMEOUT anyway.
        ex.shutdown(wait=False)

    # Zero-touch first (the concurrent gather loses the input ordering), then dedup + cap.
    collected.sort(key=lambda j: _TOUCH_RANK.get(_captcha_touch(j.get("company", ""), j.get("platform", "")), 1))
    # PER-PLATFORM QUOTA so an abundant platform can't starve a scarce one. With ~185 GH
    # postings and a global cap of 120, pure zero-touch-first ordering filled every slot
    # with Greenhouse and Lever NEVER entered the pool (live 2026-07-29: lever stayed 0
    # even after adding marketing-heavy Lever boards). Reserve a minimum share per platform;
    # if a platform doesn't use its quota, later platforms still fill the global cap below.
    # Share the cap across however many platforms are actually in this sweep, so adding a
    # platform (e.g. Workday) can't be starved by zero-touch boards saturating the cap first
    # (with the old cap//2, gh+ashby alone filled 120 and lever/workday got 0).
    _platforms_present = {p for _, p in fetch_targets} or {""}
    per_platform_cap = max(cap // len(_platforms_present), 40)
    seen: set[str] = set()
    per: dict[str, int] = {}
    out: list[dict] = []
    for job in collected:
        u = job.get("apply_url")
        if not u or u in seen:
            continue
        p = job.get("platform", "")
        if per.get(p, 0) >= per_platform_cap:
            continue  # this platform hit its share — leave room for the others
        seen.add(u)
        per[p] = per.get(p, 0) + 1
        out.append(job)
        if len(out) >= cap:
            break
    # If the quota left the global cap unfilled (only one platform had supply), top up
    # with whatever's left so we never under-deliver inventory.
    if len(out) < cap:
        for job in collected:
            u = job.get("apply_url")
            if not u or u in seen:
                continue
            seen.add(u)
            out.append(job)
            if len(out) >= cap:
                break
    return out
