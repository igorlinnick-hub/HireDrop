import requests

from modules.platforms.base import JobPlatform

REMOTEOK_API = "https://remoteok.com/api"


class RemoteOKPlatform(JobPlatform):
    name = "remoteok"
    display_name = "RemoteOK"

    def scrape(self, keywords=None, location="remote", max_results=25):
        url = REMOTEOK_API
        if keywords:
            tags = ",".join(k.lower().replace(" ", "-") for k in keywords[:3])
            url = f"{REMOTEOK_API}?tags={tags}"

        try:
            response = requests.get(url, headers={"User-Agent": "HireDrop/1.0"}, timeout=15)
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

        kw_lower = [k.lower() for k in (keywords or [])]

        jobs = []
        for item in listings:
            title = item.get("position", "")
            description = item.get("description", "")
            tags = item.get("tags", [])

            if kw_lower:
                haystack = f"{title} {description} {' '.join(tags)}".lower()
                if not any(kw in haystack for kw in kw_lower):
                    continue

            job = {
                "title": title or "Unknown",
                "company": item.get("company", "Unknown"),
                "link": item.get("url", ""),
                "date": item.get("date", ""),
                "platform": self.name,
                "location": item.get("location", "Remote"),
                "job_type": "full-time",
                "tags": tags,
                "description": description,
            }
            if job["link"] and job["title"]:
                jobs.append(job)
            if len(jobs) >= max_results:
                break

        return jobs
