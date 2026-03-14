import os
import json
from database.db import job_exists

PROFILE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "profile.json")


def load_profile():
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, "r") as f:
            return json.load(f)
    return {"keywords": [], "location": "remote", "job_type": "full-time"}


def filter_jobs(jobs):
    profile = load_profile()
    keywords = [k.lower() for k in profile.get("keywords", [])]
    location_pref = profile.get("location", "").lower()
    job_type_pref = profile.get("job_type", "").lower()

    filtered = []
    for job in jobs:
        if job_exists(job["link"]):
            continue
        # Keyword match: check title, tags, and description
        searchable = (
            job["title"] + " " +
            " ".join(job.get("tags", [])) + " " +
            job.get("description", "")
        ).lower()
        if keywords and not any(kw in searchable for kw in keywords):
            continue
        # Location filter
        job_location = job.get("location", "").lower()
        if location_pref == "remote" and job_location and job_location not in ("remote", "anywhere", ""):
            continue
        # Job type filter
        job_type = job.get("job_type", "").lower()
        if job_type_pref and job_type and job_type_pref not in job_type:
            continue
        filtered.append(job)
    return filtered
