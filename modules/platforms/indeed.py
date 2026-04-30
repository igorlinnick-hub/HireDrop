import requests
from bs4 import BeautifulSoup

from modules.platforms.base import JobPlatform


class IndeedPlatform(JobPlatform):
    name = "indeed"
    display_name = "Indeed"

    def scrape(self, keywords=None, location="remote", max_results=25):
        query = "+".join(keywords) if keywords else "developer"
        url = f"https://www.indeed.com/jobs?q={query}&l={location}&limit={max_results}"

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
        except requests.RequestException as e:
            print(f"[indeed] Request failed: {e}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        job_cards = soup.find_all("div", class_="job_seen_beacon")

        jobs = []
        for card in job_cards[:max_results]:
            title_el = card.find("h2", class_="jobTitle")
            company_el = card.find("span", attrs={"data-testid": "company-name"})
            location_el = card.find("div", attrs={"data-testid": "text-location"})
            link_el = card.find("a", href=True)
            snippet_el = card.find("div", class_="job-snippet")

            if not title_el or not link_el:
                continue

            href = link_el.get("href", "")
            if href.startswith("/"):
                href = "https://www.indeed.com" + href

            job = {
                "title": title_el.get_text(strip=True),
                "company": company_el.get_text(strip=True) if company_el else "Unknown",
                "link": href,
                "date": "",
                "platform": self.name,
                "location": location_el.get_text(strip=True) if location_el else "",
                "job_type": "",
                "tags": [],
                "description": snippet_el.get_text(strip=True) if snippet_el else "",
            }
            jobs.append(job)

        return jobs
