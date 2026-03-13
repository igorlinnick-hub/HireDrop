import requests
MAX_RESULTS = 50

REMOTEOK_API = "https://remoteok.com/api"


def scrape_jobs():
    try:
        response = requests.get(REMOTEOK_API, headers={"User-Agent": "JobFlow/1.0"}, timeout=15)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        print(f"[scraper] Request failed: {e}")
        return []
    except ValueError:
        print("[scraper] Failed to parse JSON response")
        return []

    # First item is a metadata/legal notice object, skip it
    listings = data[1:] if len(data) > 1 else []

    jobs = []
    for item in listings[:MAX_RESULTS]:
        job = {
            "title": item.get("position", "Unknown"),
            "company": item.get("company", "Unknown"),
            "link": item.get("url", ""),
            "date": item.get("date", ""),
        }
        if job["link"] and job["title"]:
            jobs.append(job)

    return jobs
