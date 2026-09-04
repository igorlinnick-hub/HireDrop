import requests

from modules.platforms.base import JobPlatform

REMOTEOK_API = "https://remoteok.com/api"


class RemoteOKPlatform(JobPlatform):
    name = "remoteok"
    display_name = "RemoteOK"

    def scrape(self, keywords=None, location="remote", max_results=25):
        # We ask for the WHOLE feed and filter locally instead of using ?tags=.
        # Why: the tag query hyphenated a multi-word keyword into one tag, and RemoteOK has
        # no "marketing-manager" tag — it answered with the header row and nothing else, so
        # every user whose keyword was a phrase got ZERO jobs from the DEFAULT platform, with
        # no error anywhere (measured 2026-09-03: "marketing manager" -> 0, "marketing" -> 10).
        # The full feed is one request of ~100 postings; local filtering is both cheaper to
        # reason about and immune to RemoteOK's tag vocabulary.
        try:
            response = requests.get(
                REMOTEOK_API, headers={"User-Agent": "HireDrop/1.0"}, timeout=15
            )
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

        # A keyword is a PHRASE: "marketing manager" matches a posting that mentions both
        # words, in any order — not one that contains that exact string. Across keywords it
        # is still OR, so "designer" or "marketing manager" both let a posting through.
        kw_terms = [[w for w in k.lower().split() if w] for k in (keywords or [])]
        kw_terms = [terms for terms in kw_terms if terms]

        jobs = []
        for item in listings:
            title = item.get("position", "")
            description = item.get("description", "")
            tags = item.get("tags", [])

            if kw_terms:
                haystack = f"{title} {description} {' '.join(tags)}".lower()
                if not any(all(w in haystack for w in terms) for terms in kw_terms):
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
