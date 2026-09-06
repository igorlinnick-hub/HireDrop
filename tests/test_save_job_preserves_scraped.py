"""save_job must never erase what discovery already scraped.

The upsert key is (user_id, link), and PostgREST derives ON CONFLICT DO UPDATE from the
keys present in the payload. save_job's optional fields default to "", so passing them
unconditionally meant a caller that has no description overwrites the stored one.

/applications/save is exactly such a caller — so every submit from the extension wiped the
posting text of the job just applied to. `description` feeds cover letters, ai_job_scorer
and ai_fit_judge, which is why this was a slow quality leak and not just a lost column.
"""

from unittest.mock import patch

import pytest

from app.db import jobs as jobs_db

ALLOWED = {
    "allowed": True,
    "reason": "",
    "tier": "pro",
    "used_today": 0,
    "daily_limit": 30,
    "free_used": None,
    "free_limit": None,
}


def _payload(supabase_mock) -> dict:
    return supabase_mock.table.return_value.upsert.call_args[0][0]


def _arm(supabase_mock) -> None:
    supabase_mock.table.return_value.upsert.return_value.execute.return_value.data = [
        {"id": "job-1"}
    ]


# ── the round trip that used to lose the posting ─────────────────────────────


class FakeJobsTable:
    """Postgres upsert semantics, only as far as this test needs them: rows keyed by
    (user_id, link), and a conflicting write updates ONLY the columns it supplies."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self._pending: dict | None = None

    def upsert(self, payload: dict, on_conflict: str = ""):
        self._pending = payload
        return self

    def execute(self):
        payload = self._pending or {}
        key = (payload["user_id"], payload["link"])
        row = self.rows.setdefault(key, {"id": "job-1"})
        row.update(payload)  # only the supplied columns move
        return type("Res", (), {"data": [row]})()


def test_applying_to_a_discovered_job_keeps_its_description(supabase_mock):
    """Discovery saves the posting, the user applies, the posting is still there."""
    table = FakeJobsTable()
    supabase_mock.table.return_value = table

    jobs_db.save_job(
        user_id="u1",
        title="Backend Engineer",
        company="Acme",
        link="https://example.com/job",
        description="We need Python and Postgres experience.",
        location="Remote",
    )
    # The apply path: same job, no description in hand.
    jobs_db.save_job(
        user_id="u1",
        title="Backend Engineer",
        company="Acme",
        link="https://example.com/job",
        status="applied",
        platform="indeed",
    )

    stored = table.rows[("u1", "https://example.com/job")]
    assert stored["description"] == "We need Python and Postgres experience."
    assert stored["location"] == "Remote"
    assert stored["status"] == "applied"  # the apply still updates what it owns


# ── payload shape ────────────────────────────────────────────────────────────


def test_submit_without_description_omits_it_from_the_upsert(supabase_mock):
    _arm(supabase_mock)

    jobs_db.save_job(user_id="u1", title="T", company="C", link="https://x", platform="indeed")

    payload = _payload(supabase_mock)
    assert "description" not in payload
    assert "location" not in payload
    assert "job_type" not in payload
    assert payload["title"] == "T"
    assert payload["status"] == "new"


def test_discovery_still_writes_what_it_scraped(supabase_mock):
    _arm(supabase_mock)

    jobs_db.save_job(
        user_id="u1",
        title="T",
        company="C",
        link="https://x",
        description="Real posting text",
        location="Remote",
        job_type="Full-time",
    )

    payload = _payload(supabase_mock)
    assert payload["description"] == "Real posting text"
    assert payload["location"] == "Remote"
    assert payload["job_type"] == "Full-time"


@pytest.mark.parametrize("blank", ["", None])
def test_blank_values_are_treated_as_absent(supabase_mock, blank):
    """A caller passing an empty value means "I don't have this", never "store empty"."""
    _arm(supabase_mock)

    jobs_db.save_job(
        user_id="u1", title="T", company="C", link="https://x", description=blank, location=blank
    )

    payload = _payload(supabase_mock)
    assert "description" not in payload
    assert "location" not in payload


# ── the real caller ──────────────────────────────────────────────────────────


def test_submitting_an_application_sends_no_description(auth_client):
    """End of the regression path: the apply endpoint is the caller with no description,
    so this is where the column used to get wiped."""
    with (
        patch("app.routers.applications.check_can_apply", return_value=ALLOWED),
        patch("app.db.jobs.save_job", return_value="job-1") as save_job,
        patch("app.db.applications.save_application", return_value="app-1"),
    ):
        res = auth_client.post(
            "/api/v1/applications/save",
            json={
                "job_title": "Backend Engineer",
                "company": "Acme",
                "job_url": "https://example.com/job",
                "platform": "indeed",
                "cover_letter": "",
                "status": "applied",
            },
        )

    assert res.status_code == 200
    assert "description" not in save_job.call_args.kwargs
