import requests

from modules.platforms.base import JobPlatform

REMOTEOK_API = "https://remoteok.com/api"


class RemoteOKPlatform(JobPlatform):
    name = "remoteok"
    display_name = "RemoteOK"

    def scrape(self, keywords=None, location="remote", max_results=25):
        try:
            response = requests.get(REMOTEOK_API, headers={"User-Agent": "JobFlow/1.0"}, timeout=15)
            response.raise_for_status()
            response.encoding = "utf-8"
            data = response.json()
        except requests.RequestException as e:
            print(f"[remoteok] Request failed: {e}")
            return []
        except ValueError:
            print("[remoteok] Failed to parse JSON")
            return []

        listings = data[1:] if len(data) > 1 else []

        jobs = []
        for item in listings[:max_results]:
            job = {
                "title": item.get("position", "Unknown"),
                "company": item.get("company", "Unknown"),
                "link": item.get("url", ""),
                "date": item.get("date", ""),
                "platform": self.name,
                "location": item.get("location", "Remote"),
                "job_type": "full-time",
                "tags": item.get("tags", []),
                "description": item.get("description", ""),
            }
            if job["link"] and job["title"]:
                jobs.append(job)

        return jobs
