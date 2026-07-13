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

import requests

GREENHOUSE_BOARD_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
LEVER_POSTINGS_API = "https://api.lever.co/v0/postings/{token}?mode=json"

_UA = {"User-Agent": "HireDrop/1.0 (+https://hiredrop.io)"}
_TIMEOUT = 12

# Hosts phase_ats can actually fill (detectPlatform recognizes greenhouse.io / lever.co).
# Some companies (stripe, databricks, coinbase…) embed Greenhouse but expose the job on
# their OWN career domain (stripe.com/jobs?gh_jid=…) — the board API still lists those, but
# their apply_url isn't a form phase_ats handles, so we drop them here rather than hand the
# filler a URL it can't drive. (Verified 2026-07-12: ~half of GH matches are custom-domain.)
_FILLABLE_SUFFIXES = ("greenhouse.io", "lever.co")


def _is_fillable(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        h = urlparse(url).hostname or ""
        return any(h == d or h.endswith("." + d) for d in _FILLABLE_SUFFIXES)
    except Exception:
        return False


def _keyword_match(text: str, keywords: list[str] | None) -> bool:
    """No keywords -> match everything. Otherwise ANY keyword substring (case-insensitive)."""
    if not keywords:
        return True
    t = (text or "").lower()
    return any(k.strip().lower() in t for k in keywords if k.strip())


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
        out.append(_job(title, token, url, loc, "greenhouse"))
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
        out.append(_job(title, token, url, loc, "lever", p.get("descriptionPlain", "")))
        if len(out) >= limit:
            break
    return out


_FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever}


def discover_ats(companies: list[tuple[str, str]], keywords: list[str] | None = None,
                 cap: int = 100) -> list[dict]:
    """Pull live jobs across a watchlist of (token, platform) companies, keyword-filtered.

    Dedups by apply_url. `companies` = [("airtable", "greenhouse"), ("shieldai", "lever"), ...].
    Low-captcha platforms are queried FIRST so the cap fills with zero-touch destinations
    before human-touch ones — the campaign then applies to the easy (no-captcha) wins first.
    """
    companies = sorted(companies, key=lambda c: _TOUCH_RANK.get(_captcha_touch(c[0], c[1]), 1))
    seen: set[str] = set()
    out: list[dict] = []
    for token, platform in companies:
        fetch = _FETCHERS.get(platform)
        if not fetch:
            continue
        for job in fetch(token, keywords):
            u = job["apply_url"]
            if u in seen:
                continue
            seen.add(u)
            out.append(job)
            if len(out) >= cap:
                return out
    return out
