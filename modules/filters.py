from database.db import job_exists
from config import SEARCH_KEYWORDS


def filter_jobs(jobs):
    filtered = []
    for job in jobs:
        if job_exists(job["link"]):
            continue
        title_lower = job["title"].lower()
        if not any(keyword.lower() in title_lower for keyword in SEARCH_KEYWORDS):
            continue
        filtered.append(job)
    return filtered
