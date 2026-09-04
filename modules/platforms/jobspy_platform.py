"""JobSpy-backed platform scrapers: LinkedIn, Glassdoor, ZipRecruiter, Google Jobs."""

from modules.platforms.base import JobPlatform

LOCATION_MAP = {
    "remote": "Remote",
    "usa": "United States",
    "europe": "Europe",
}


def _normalize_df(df, platform_name: str) -> list[dict]:
    """Convert a JobSpy DataFrame to HireDrop normalized job dicts."""
    jobs = []
    for _, row in df.iterrows():
        title = str(row.get("title", "") or "")
        company = str(row.get("company", "") or "")
        link = str(row.get("job_url", "") or "")
        description = str(row.get("description", "") or "")
        location = str(row.get("location", "") or "")
        job_type = str(row.get("job_type", "") or "")
        date = str(row.get("date_posted", "") or "")

        if not title or not link:
            continue

        jobs.append(
            {
                "title": title,
                "company": company,
                "link": link,
                "date": date,
                "platform": platform_name,
                "location": location,
                "job_type": job_type,
                "tags": [],
                "description": description,
            }
        )
    return jobs


class LinkedInPlatform(JobPlatform):
    name = "linkedin"
    display_name = "LinkedIn"
    requires_credentials = False

    def scrape(self, keywords=None, location="remote", max_results=25):
        try:
            from jobspy import scrape_jobs

            df = scrape_jobs(
                site_name=["linkedin"],
                search_term=" ".join(keywords or []),
                location=LOCATION_MAP.get(location, location),
                results_wanted=max_results,
                linkedin_fetch_description=False,
            )
            return _normalize_df(df, self.name)
        except Exception as e:
            print(f"[linkedin] Scrape failed: {e}")
            return []


class GlassdoorPlatform(JobPlatform):
    """Dead server-side since at least 2026-09-03, and already off the website roster.

    Every request answers HTTP 400 + "Glassdoor: location not parsed" — for `remote`,
    `United States` and `New York, NY` alike, so it is not a location-format problem we
    could patch. Kept registered (some profiles still carry "glassdoor" in profile.platforms)
    but marked, so those users get a reason instead of a silent zero.
    Re-check: scripts/audit_discovery_sources.py.
    """

    name = "glassdoor"
    display_name = "Glassdoor"
    requires_credentials = False
    unavailable_reason = (
        "Glassdoor search is unavailable — its API rejects our requests. Its jobs mostly "
        "duplicate Indeed, which we do search."
    )

    def scrape(self, keywords=None, location="remote", max_results=25):
        if self.unavailable_reason:
            print(f"[glassdoor] skipped — {self.unavailable_reason}")
            return []
        try:
            from jobspy import scrape_jobs

            df = scrape_jobs(
                site_name=["glassdoor"],
                search_term=" ".join(keywords or []),
                location=LOCATION_MAP.get(location, location),
                results_wanted=max_results,
            )
            return _normalize_df(df, self.name)
        except Exception as e:
            print(f"[glassdoor] Scrape failed: {e}")
            return []


class ZipRecruiterPlatform(JobPlatform):
    name = "ziprecruiter"
    display_name = "ZipRecruiter"
    requires_credentials = False

    def scrape(self, keywords=None, location="remote", max_results=25):
        try:
            from jobspy import scrape_jobs

            df = scrape_jobs(
                site_name=["zip_recruiter"],
                search_term=" ".join(keywords or []),
                location=LOCATION_MAP.get(location, location),
                results_wanted=max_results,
            )
            return _normalize_df(df, self.name)
        except Exception as e:
            print(f"[ziprecruiter] Scrape failed: {e}")
            return []


class GoogleJobsPlatform(JobPlatform):
    """Google Jobs (Google for Jobs) — DISCOVERY ONLY, and currently unreachable server-side.

    Two separate facts, both re-checked live 2026-09-03 (docs/handoff/google-jobs.md):

    1. Google for Jobs never receives an application. Every card links OUT to the source
       (LinkedIn / Indeed / ZipRecruiter / Greenhouse / Lever / Ashby / Workday / a company
       career page), so it can only ever feed the pool — it can never be an apply platform.
    2. Google Search no longer answers a non-JS HTTP client. `GET /search?udm=8` returns the
       "enablejs" redirect interstitial — 200 OK, ~93 KB, no job payload, no captcha — for
       every variant tried (plain, consent cookie, gl/hl, TLS-fingerprinted client) on the
       LATEST python-jobspy (1.1.82). So JobSpy's google module yields [] every single time.

    Kept rather than deleted: the scrape path below is correct the moment the gate lifts, and
    `scripts/probe_google_jobs.py` exits 0 on the day it does. Deleting it is also how
    "let's add Google Jobs" comes back as a fresh idea next quarter.
    """

    name = "google"
    display_name = "Google Jobs"
    requires_credentials = False
    unavailable_reason = (
        "Google Jobs is paused: Google now serves job results only to a real browser, "
        "so we can't search it from our server."
    )

    def scrape(self, keywords=None, location="remote", max_results=25):
        if self.unavailable_reason:
            # Never spend a request on a known-dead source; never return a silent zero.
            print(f"[google] skipped — {self.unavailable_reason}")
            return []
        try:
            from jobspy import scrape_jobs

            df = scrape_jobs(
                site_name=["google"],
                search_term=" ".join(keywords or []),
                location=LOCATION_MAP.get(location, location),
                results_wanted=max_results,
                google_search_term=" ".join(keywords or []) + " jobs",
            )
            return _normalize_df(df, self.name)
        except Exception as e:
            print(f"[google] Scrape failed: {e}")
            return []
