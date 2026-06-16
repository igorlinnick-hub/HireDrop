"""Craigslist jobs scraper — searches across major US cities in parallel."""

import concurrent.futures
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from modules.platforms.base import JobPlatform

# Top 20 US Craigslist cities by job market size
US_CITIES = [
    "newyork", "sfbay", "losangeles", "chicago", "seattle",
    "boston", "austin", "denver", "miami", "atlanta",
    "dallas", "portland", "phoenix", "sandiego", "minneapolis",
    "houston", "philadelphia", "detroit", "baltimore", "nashville",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _scrape_city(city: str, query: str, remote: bool) -> list[dict]:
    params = {"query": query, "sort": "date"}
    if remote:
        params["query"] = f"{query} remote"

    url = f"https://{city}.craigslist.org/search/jjj?{urlencode(params)}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # New Craigslist layout (2023+)
    jobs = []
    for item in soup.select("li.cl-static-search-result"):
        a = item.select_one("a")
        if not a:
            continue
        href = a.get("href", "")
        if not href.startswith("http"):
            href = f"https://{city}.craigslist.org{href}"
        title = a.get_text(strip=True)
        if not title or not href:
            continue
        jobs.append({
            "title": title,
            "company": city.capitalize(),
            "link": href,
            "date": "",
            "platform": "craigslist",
            "location": city,
            "job_type": "",
            "tags": [],
            "description": "",
        })

    # Fallback: older Craigslist layout
    if not jobs:
        for item in soup.select(".result-row"):
            a = item.select_one(".result-title")
            if not a:
                continue
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if not title or not href:
                continue
            jobs.append({
                "title": title,
                "company": city.capitalize(),
                "link": href,
                "date": "",
                "platform": "craigslist",
                "location": city,
                "job_type": "",
                "tags": [],
                "description": "",
            })

    return jobs


class CraigslistPlatform(JobPlatform):
    name = "craigslist"
    display_name = "Craigslist"
    requires_credentials = False

    def scrape(self, keywords=None, location="remote", max_results=50):
        query = " ".join(keywords or ["jobs"])
        remote = location == "remote"

        # Scrape all cities in parallel (max 8 workers, 10s timeout per city)
        all_jobs: list[dict] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(_scrape_city, city, query, remote): city
                for city in US_CITIES
            }
            for future in concurrent.futures.as_completed(futures, timeout=20):
                try:
                    all_jobs.extend(future.result())
                except Exception as e:
                    print(f"[craigslist] City scrape error: {e}")

        # Deduplicate by link, cap at max_results
        seen = set()
        unique: list[dict] = []
        for job in all_jobs:
            if job["link"] not in seen:
                seen.add(job["link"])
                unique.append(job)
            if len(unique) >= max_results:
                break

        return unique
