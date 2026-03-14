import requests
from bs4 import BeautifulSoup
from modules.platforms.base import JobPlatform


class WellfoundPlatform(JobPlatform):
    name = "wellfound"
    display_name = "Wellfound"

    def scrape(self, keywords=None, location="remote", max_results=25):
        query = "-".join(keywords) if keywords else "developer"
        url = f"https://wellfound.com/role/r/{query}"

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
        except requests.RequestException as e:
            print(f"[wellfound] Request failed: {e}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        job_cards = soup.find_all("div", class_="styles_result__rPRNG")

        jobs = []
        for card in job_cards[:max_results]:
            title_el = card.find("a", class_="styles_defaultLink__dpIOd")
            company_el = card.find("h2")
            location_el = card.find("span", class_="styles_location__Ej0tN")

            if not title_el:
                continue

            href = title_el.get("href", "")
            if href.startswith("/"):
                href = "https://wellfound.com" + href

            job = {
                "title": title_el.get_text(strip=True),
                "company": company_el.get_text(strip=True) if company_el else "Unknown",
                "link": href,
                "date": "",
                "platform": self.name,
                "location": location_el.get_text(strip=True) if location_el else "",
                "job_type": "",
                "tags": [],
                "description": "",
            }
            jobs.append(job)

        return jobs
