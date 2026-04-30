"""Pure keyword/location/job_type filter for scraped job listings.

Single responsibility: takes a list of normalized job dicts + a profile and
returns the subset matching the profile's preferences. No I/O, no storage.

Deduplication against previously-saved jobs is done by the caller via
app.db.jobs.job_exists, not here.
"""


def filter_jobs(jobs: list[dict], profile: dict) -> list[dict]:
    keywords = [k.lower() for k in profile.get("keywords", [])]
    location_pref = (profile.get("location") or "").lower()
    job_type_pref = (profile.get("job_type") or "").lower()

    filtered = []
    for job in jobs:
        searchable = (
            (job.get("title") or "")
            + " "
            + " ".join(job.get("tags", []) or [])
            + " "
            + (job.get("description") or "")
        ).lower()
        if keywords and not any(kw in searchable for kw in keywords):
            continue

        job_location = (job.get("location") or "").lower()
        if (
            location_pref == "remote"
            and job_location
            and job_location not in ("remote", "anywhere", "")
        ):
            continue

        job_type = (job.get("job_type") or "").lower()
        if job_type_pref and job_type and job_type_pref not in job_type:
            continue

        filtered.append(job)
    return filtered
