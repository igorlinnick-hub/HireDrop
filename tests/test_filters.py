"""Unit tests for the pure filter_jobs function.

After Phase 2 refactor, filter_jobs has no I/O — these tests don't need
fixtures or mocks.
"""

from modules.filters import filter_jobs


def _job(**kwargs):
    base = {
        "title": "",
        "company": "",
        "link": "",
        "tags": [],
        "description": "",
        "location": "",
        "job_type": "",
    }
    base.update(kwargs)
    return base


def test_no_keywords_returns_all():
    jobs = [_job(title="Frontend"), _job(title="Backend")]
    assert filter_jobs(jobs, {"keywords": []}) == jobs


def test_keyword_match_in_title():
    jobs = [_job(title="Marketing Lead"), _job(title="Software Engineer")]
    out = filter_jobs(jobs, {"keywords": ["marketing"]})
    assert len(out) == 1
    assert out[0]["title"] == "Marketing Lead"


def test_keyword_match_in_tags():
    jobs = [_job(title="Engineer", tags=["python", "fastapi"])]
    out = filter_jobs(jobs, {"keywords": ["fastapi"]})
    assert len(out) == 1


def test_keyword_match_in_description():
    jobs = [_job(title="X", description="Looking for a content writer")]
    out = filter_jobs(jobs, {"keywords": ["content"]})
    assert len(out) == 1


def test_keyword_case_insensitive():
    jobs = [_job(title="MARKETING")]
    out = filter_jobs(jobs, {"keywords": ["marketing"]})
    assert len(out) == 1


def test_location_remote_filters_out_onsite():
    jobs = [
        _job(title="X", location="Remote"),
        _job(title="X", location="New York"),
        _job(title="X", location=""),
    ]
    out = filter_jobs(jobs, {"keywords": [], "location": "remote"})
    assert len(out) == 2  # Remote + empty pass


def test_location_remote_keeps_anywhere():
    jobs = [_job(title="X", location="Anywhere")]
    out = filter_jobs(jobs, {"keywords": [], "location": "remote"})
    assert len(out) == 1


def test_job_type_filter():
    jobs = [
        _job(title="X", job_type="full-time"),
        _job(title="X", job_type="contract"),
    ]
    out = filter_jobs(jobs, {"keywords": [], "job_type": "full-time"})
    assert len(out) == 1
    assert out[0]["job_type"] == "full-time"


def test_handles_none_values_gracefully():
    """tags=None or description=None must not crash."""
    jobs = [_job(title="Marketing", tags=None, description=None)]
    out = filter_jobs(jobs, {"keywords": ["marketing"]})
    assert len(out) == 1


def test_generic_talent_pools_are_not_vacancies():
    """Live pool 2026-09-21: 8 catch-all rows, 3 already submitted. They pass the keyword
    filter honestly — it matches distinctive WORDS, so "health care" matches "FOLX Health
    Talent Community" and "social media" matches "Vox Media". "Is this a job at all" is a
    separate question, so it gets its own gate."""
    from modules.platforms.ats_boards import is_generic_talent_pool, keyword_match

    kws = ["event manager", "social media", "promotion", "health care"]
    caught = [
        "Join the FOLX Health Talent Community",
        "Don't see your dream job here? Apply to Vox Media for future roles",
        "Physician - Full-Spectrum Primary Care - NYC - Talent Community",
        "General Application",
        "Introduce Yourself",
        "Open Application — Marketing",
        "Future Openings at Acme",
    ]
    for title in caught:
        # each one really does pass the keyword filter — that is why the gate is needed
        assert is_generic_talent_pool(title), title

    kept = [
        "Social Media Manager",
        "Event Manager, Brand Partnerships",
        "Community Manager",  # a real role — "community" alone must not trip the gate
        "Talent Acquisition Partner",  # recruiting IS a job
        "Health Care Marketing Lead",
    ]
    for title in kept:
        assert not is_generic_talent_pool(title), title
    # The gate is independent of the keyword filter: it must not change what matches.
    for title in ("Social Media Manager", "Event Manager, Brand Partnerships"):
        assert keyword_match(title, kws), title
