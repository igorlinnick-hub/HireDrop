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

        jobs.append({
            "title": title,
            "company": company,
            "link": link,
            "date": date,
            "platform": platform_name,
            "location": location,
            "job_type": job_type,
            "tags": [],
            "description": description,
        })
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
    name = "glassdoor"
    display_name = "Glassdoor"
    requires_credentials = False

    def scrape(self, keywords=None, location="remote", max_results=25):
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
    name = "google"
    display_name = "Google Jobs"
    requires_credentials = False

    def scrape(self, keywords=None, location="remote", max_results=25):
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
