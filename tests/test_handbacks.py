"""Hand-backs as a live to-do list — the applications waiting on the user's hands.

Born 09-21: the filler's hand-back existed only as an activity-log line, so the ONE
thing in the product that needs a human was the hardest thing to find. Igor: "это нужно
не в history а в попапе, чтоб была анимация заявки что нужно человеку доделать". Two
surfaces read this now (extension popup + dashboard rail), which is exactly why the
open/done state needs one owner.

These tests pin the two properties that make the list trustworthy: it counts JOBS not
attempts, and it drains.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.db import handbacks as hb

# conftest stubs open_urls for every test (the queue must not reach the network); these
# tests exercise the real one, captured at import — before that fixture runs.
_real_open_urls = hb.open_urls


@pytest.fixture(autouse=True)
def _no_build_read():
    """add() and open_urls() read the running build; a test that cares patches it itself."""
    with patch.object(hb, "_current_build", return_value=(None, None)):
        yield


class _User:
    id = "u1"
    email = "u1@example.com"


def _fake_supabase():
    """Chainable stub — records the filters the query actually applied."""
    calls = {"filters": [], "table": None, "payload": None, "conflict": None}
    tbl = MagicMock()

    def _remember(name):
        def f(*args, **kwargs):
            calls["filters"].append((name, args))
            return tbl

        return f

    tbl.select.side_effect = _remember("select")
    tbl.eq.side_effect = _remember("eq")
    tbl.is_.side_effect = _remember("is_")
    tbl.order.side_effect = _remember("order")
    tbl.limit.side_effect = _remember("limit")

    def _upsert(row, **kwargs):
        calls["payload"] = row
        calls["conflict"] = kwargs.get("on_conflict")
        return tbl

    tbl.upsert.side_effect = _upsert

    def _insert(row):
        calls["inserted"] = row
        calls["payload"] = row
        return tbl

    tbl.insert.side_effect = _insert

    def _update(patch_):
        calls["payload"] = patch_
        return tbl

    tbl.update.side_effect = _update

    tbl.execute.return_value = MagicMock(data=[{"id": "h1"}])
    client = MagicMock()

    def _table(name):
        calls["table"] = name
        return tbl

    client.table.side_effect = _table
    return client, calls, tbl


def test_open_list_is_scoped_to_the_user_and_hides_resolved():
    """service_role bypasses RLS, so a missing user_id filter hands over someone else's
    job links — the project's #1 risk class."""
    client, calls, _tbl = _fake_supabase()
    with patch.object(hb, "get_supabase", return_value=client):
        hb.list_open("u1")

    assert calls["table"] == "handbacks"
    assert ("eq", ("user_id", "u1")) in calls["filters"]
    assert ("is_", ("resolved_at", "null")) in calls["filters"]


def test_the_same_job_handed_back_twice_does_not_stack():
    """The list must count JOBS waiting, not attempts — a re-run that hits the same
    wall would otherwise inflate the badge and teach the user to ignore it."""
    client, calls, _tbl = _fake_supabase()  # the update finds the open row
    with patch.object(hb, "get_supabase", return_value=client):
        hb.add("u1", {"job_title": "Assoc. Director", "url": "https://x/form"})

    assert "inserted" not in calls
    assert ("eq", ("user_id", "u1")) in calls["filters"]
    assert ("eq", ("url", "https://x/form")) in calls["filters"]
    # Only the OPEN row — a resolved one stays history, the job starts a new row.
    assert ("is_", ("resolved_at", "null")) in calls["filters"]


def test_a_new_job_is_inserted_not_upserted():
    """The uniqueness is a PARTIAL index; Postgres rejects ON CONFLICT (user_id, url)
    against it with 42P10. That upsert made every POST /handbacks a 500 for six days
    while this mock happily accepted it — so pin that the path never goes back to it."""
    client, calls, tbl = _fake_supabase()
    tbl.execute.side_effect = [MagicMock(data=[]), MagicMock(data=[{"id": "h9"}])]
    with patch.object(hb, "get_supabase", return_value=client):
        row = hb.add("u1", {"job_title": "PM", "url": "https://x/new"})

    assert row == {"id": "h9"}
    assert calls["inserted"]["url"] == "https://x/new"
    assert not tbl.upsert.called


def test_a_racing_duplicate_becomes_an_update():
    """Two reports of one wall can both miss the lookup; the index admits the first,
    and the second must land as an update of it, not an error the extension swallows."""
    from postgrest.exceptions import APIError

    client, calls, tbl = _fake_supabase()
    dup = APIError({"code": "23505", "message": "duplicate key"})
    tbl.execute.side_effect = [MagicMock(data=[]), dup, MagicMock(data=[{"id": "h1"}])]
    with patch.object(hb, "get_supabase", return_value=client):
        assert hb.add("u1", {"url": "https://x/form"}) == {"id": "h1"}


def test_resolve_is_scoped_and_only_touches_still_open_rows():
    client, calls, _tbl = _fake_supabase()
    with patch.object(hb, "get_supabase", return_value=client):
        assert hb.resolve("u1", "h1") is True

    assert ("eq", ("user_id", "u1")) in calls["filters"]
    assert ("eq", ("id", "h1")) in calls["filters"]
    # Re-resolving must not move resolved_at a second time.
    assert ("is_", ("resolved_at", "null")) in calls["filters"]
    assert "resolved_at" in calls["payload"]


def test_resolving_something_that_isnt_yours_reports_plain_false():
    """A wrong id and someone else's id must answer identically — a distinguishable
    error confirms another user's row exists."""
    client, _, tbl = _fake_supabase()
    tbl.execute.return_value = MagicMock(data=[])  # no row matched
    with patch.object(hb, "get_supabase", return_value=client):
        assert hb.resolve("u1", "not-mine") is False


def test_long_fields_are_truncated_not_rejected():
    """A 10k-char reason from a weird form must not fail the write — losing the hand-back
    entirely is worse than losing the tail of its text."""
    client, calls, _tbl = _fake_supabase()
    with patch.object(hb, "get_supabase", return_value=client):
        hb.add("u1", {"job_title": "T" * 5000, "reason": "R" * 5000, "url": "u" * 5000})

    assert len(calls["payload"]["job_title"]) == 300
    assert len(calls["payload"]["reason"]) == 500
    assert len(calls["payload"]["url"]) == 1000


# --- an extension update OFFERS a retry; only the person sends it (Igor, 10-02) ---


def _open_rows(*rows):
    return patch.object(hb, "fetch_paged", return_value=list(rows))


def test_a_newer_build_does_not_reopen_a_hand_back_by_itself():
    """The person may have finished it by hand without pressing "done": a newer build
    re-opening it on its own would submit to the same posting twice."""
    rows = [{"url": "https://gh/old"}, {"url": "https://gh/same"}]
    with _open_rows(*rows), patch.object(hb, "_current_build") as build:
        assert _real_open_urls("u1", waiting_only=True) == ["https://gh/old", "https://gh/same"]
    build.assert_not_called()


def _listed(rows, build=("1.8.31", "2026-10-02T22:00:00+00:00")):
    client, _calls, tbl = _fake_supabase()
    tbl.execute.return_value = MagicMock(data=rows)
    with (
        patch.object(hb, "get_supabase", return_value=client),
        patch.object(hb, "_current_build", return_value=build),
    ):
        return {r["id"]: r["newer_build"] for r in hb.list_open("u1")}


def test_the_list_marks_what_an_update_could_now_finish():
    rows = [
        {
            "id": "old-gh",
            "platform": "greenhouse",
            "ext_version": "1.8.23",
            "created_at": "2026-10-01T10:00:00+00:00",
        },
        {
            "id": "same-gh",
            "platform": "greenhouse",
            "ext_version": "1.8.31",
            "created_at": "2026-10-02T23:00:00+00:00",
        },
        {
            "id": "legacy-gh",
            "platform": "greenhouse",
            "ext_version": None,
            "created_at": "2026-10-01T10:00:00+00:00",
        },
        {
            "id": "requeued",
            "platform": "greenhouse",
            "ext_version": "1.8.23",
            "requeued_at": "2026-10-02T23:01:00+00:00",
        },
        # A native Indeed hand-back has no queue row to go back to — no promise to offer.
        {
            "id": "indeed",
            "platform": "indeed",
            "ext_version": "1.8.23",
            "created_at": "2026-10-01T10:00:00+00:00",
        },
    ]
    assert _listed(rows) == {
        "old-gh": True,
        "same-gh": False,
        "legacy-gh": True,
        "requeued": False,
        "indeed": False,
    }


def test_an_unknown_build_offers_nothing():
    rows = [
        {
            "id": "a",
            "platform": "greenhouse",
            "ext_version": "1.8.3",
            "created_at": "2026-09-01T00:00:00+00:00",
        }
    ]
    assert _listed(rows, build=(None, None)) == {"a": False}


def test_retry_stamps_requeued_and_is_scoped_to_the_user():
    client, calls, _tbl = _fake_supabase()
    with patch.object(hb, "get_supabase", return_value=client):
        assert hb.retry("u1", "h1") == {"id": "h1"}
    assert "requeued_at" in calls["payload"]
    assert ("eq", ("user_id", "u1")) in calls["filters"]
    assert ("is_", ("resolved_at", "null")) in calls["filters"]


def test_a_repeat_hand_back_waits_again_and_is_restamped():
    """A re-queued job that hits the wall again must not stay re-queued — the queue
    would serve the same wall on every run."""
    client, calls, _tbl = _fake_supabase()
    with (
        patch.object(hb, "get_supabase", return_value=client),
        patch.object(hb, "_current_build", return_value=("1.8.31", "2026-10-02T22:00:00+00:00")),
    ):
        hb.add("u1", {"job_title": "Ops", "url": "https://gh/form"})
    assert calls["payload"]["ext_version"] == "1.8.31"
    assert calls["payload"]["requeued_at"] is None


def test_a_missing_column_drops_the_stamp_not_the_hand_back():
    from postgrest.exceptions import APIError

    client, calls, tbl = _fake_supabase()
    tbl.execute.side_effect = [
        APIError({"code": "PGRST204", "message": "no ext_version column"}),
        MagicMock(data=[{"id": "h1"}]),
    ]
    with (
        patch.object(hb, "get_supabase", return_value=client),
        patch.object(hb, "_current_build", return_value=("1.8.30", None)),
    ):
        assert hb.add("u1", {"url": "https://gh/form"}) == {"id": "h1"}
    assert "ext_version" not in calls["payload"]


def test_a_submitted_posting_leaves_the_to_do_list_by_identity():
    """The retry saves the posting URL; the hand-back holds the form URL of the same job."""
    client, calls, tbl = _fake_supabase()
    open_rows = [
        {"id": "h1", "url": "https://job-boards.greenhouse.io/doordashusa/jobs/8237299?gh_src=x"},
        {"id": "h2", "url": "https://job-boards.greenhouse.io/tia/jobs/8005735003"},
    ]
    tbl.in_ = MagicMock(return_value=tbl)
    with (
        patch.object(hb, "get_supabase", return_value=client),
        patch.object(hb, "fetch_paged", return_value=open_rows),
    ):
        n = hb.resolve_for_posting("u1", "https://boards.greenhouse.io/doordashusa/jobs/8237299")
    assert n == 1
    tbl.in_.assert_called_once_with("id", ["h1"])
    assert ("eq", ("user_id", "u1")) in calls["filters"]
    assert "resolved_at" in calls["payload"]


def test_an_unidentifiable_url_closes_nothing():
    with patch.object(hb, "fetch_paged") as read:
        assert hb.resolve_for_posting("u1", "") == 0
    read.assert_not_called()


def test_retry_endpoint_says_not_found_for_a_row_that_is_not_yours(auth_client):
    with patch("app.db.handbacks.retry", return_value=None):
        r = auth_client.post("/api/v1/handbacks/h-other/retry")
    assert r.status_code == 404


def test_retry_endpoint_requeues_an_ats_hand_back(auth_client):
    row = {"id": "h1", "platform": "greenhouse", "job_id": None}
    with (
        patch("app.db.handbacks.retry", return_value=row) as retry,
        patch("app.db.jobs.update_job_status") as status,
    ):
        r = auth_client.post("/api/v1/handbacks/h1/retry")
    assert r.status_code == 200 and r.json()["requeued"] is True
    retry.assert_called_once()
    status.assert_not_called()  # no pool row to flip; the ATS queue serves it as `new`


# --- Identity: one open row per JOB, never per form screen (10-06) ----------------------
#
# Every Indeed application runs through the same smartapply step URLs, and the open-row
# key is the URL — so each new Indeed hand-back overwrote the person's previous one
# (15 Indeed jobs handed back 09-28…10-05 left 5 rows). Captured step URLs from prod
# activity_log: they carry no query at all.

_STEP_REVIEW = (
    "https://smartapply.indeed.com/beta/indeedapply/form/resume-module/structured-data-review"
)
_STEP_Q1 = "https://smartapply.indeed.com/beta/indeedapply/form/questions-module/questions/1"


def _url_written(job: dict, pool_jk=None) -> str:
    client, calls, _tbl = _fake_supabase()
    with (
        patch.object(hb, "get_supabase", return_value=client),
        patch.object(hb, "_pool_jk", return_value=pool_jk),
    ):
        hb.add("u1", job)
    return calls["payload"]["url"]


def test_two_indeed_jobs_on_the_same_step_url_get_two_rows():
    """The bug: same step URL, different jobs, one row. An old extension still sends the
    step URL, so the backend must tell the jobs apart by themselves."""
    a = _url_written(
        {"url": _STEP_REVIEW, "job_title": "Event Coordinator", "company": "Bowtech Archery"}
    )
    b = _url_written(
        {"url": _STEP_REVIEW, "job_title": "Marketing Coordinator", "company": "Onvera Health"}
    )
    assert a != b
    assert "smartapply" not in a and "smartapply" not in b


def test_the_same_indeed_job_on_two_step_urls_is_one_row():
    """A re-run of one job that stops on a different screen is still that job."""
    job = {"job_title": "Event Coordinator", "company": "Bowtech Archery"}
    assert _url_written({**job, "url": _STEP_REVIEW}) == _url_written({**job, "url": _STEP_Q1})


def test_a_new_extension_posting_url_is_the_identity_in_one_spelling():
    """ext >= this fix sends the /viewjob URL the walk opened; query spellings collapse."""
    a = _url_written({"url": "https://www.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b&from=serp&tk=x"})
    b = _url_written({"url": "https://www.indeed.com/jobs?q=pm&vjk=1A2B3C4D5E6F7A8B"})
    assert a == b == "https://www.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b"


def test_an_old_step_url_resolves_to_the_pool_posting_when_unambiguous():
    url = _url_written(
        {"url": _STEP_REVIEW, "job_title": "Event Coordinator", "company": "Bowtech Archery"},
        pool_jk="aaaaaaaaaaaaaaaa",
    )
    assert url == "https://www.indeed.com/viewjob?jk=aaaaaaaaaaaaaaaa"


def test_an_old_step_url_without_a_pool_match_becomes_a_search_link():
    url = _url_written(
        {"url": _STEP_REVIEW, "job_title": "Event Coordinator", "company": "Bowtech Archery"}
    )
    assert url == "https://www.indeed.com/jobs?q=Event+Coordinator+Bowtech+Archery"


def test_a_step_url_with_nothing_to_identify_the_job_stays_as_sent():
    """No title, no company: nothing to key on, so the old behaviour — and those anonymous
    rows can no longer swallow a named job's row."""
    assert _url_written({"url": _STEP_REVIEW}) == _STEP_REVIEW


def test_ats_urls_are_stored_as_sent():
    for u in (
        "https://job-boards.greenhouse.io/doordashusa/jobs/8237299",
        "https://jobs.lever.co/acme/1234-abcd/apply",
        "https://www.ziprecruiter.com/jobs-search?q=pm&lk=abc",
    ):
        assert _url_written({"url": u, "job_title": "PM", "company": "Acme"}) == u


def test_the_pool_lookup_is_scoped_to_the_user_and_refuses_to_guess():
    client, calls, tbl = _fake_supabase()
    tbl.execute.return_value = MagicMock(
        data=[
            {"link": "https://www.indeed.com/viewjob?jk=aaaaaaaaaaaaaaaa"},
            {"link": "https://www.indeed.com/viewjob?jk=AAAAAAAAAAAAAAAA&from=serp"},
        ]
    )
    with patch.object(hb, "get_supabase", return_value=client):
        assert hb._pool_jk("u1", "Event Coordinator", "Bowtech Archery") == "aaaaaaaaaaaaaaaa"
    assert calls["table"] == "jobs"
    assert ("eq", ("user_id", "u1")) in calls["filters"]

    # Two postings with this title + company (two locations, a repost): no guess.
    tbl.execute.return_value = MagicMock(
        data=[
            {"link": "https://www.indeed.com/viewjob?jk=aaaaaaaaaaaaaaaa"},
            {"link": "https://www.indeed.com/viewjob?jk=bbbbbbbbbbbbbbbb"},
        ]
    )
    with patch.object(hb, "get_supabase", return_value=client):
        assert hb._pool_jk("u1", "Event Coordinator", "Bowtech Archery") is None


def test_a_failed_pool_lookup_falls_back_instead_of_raising():
    broken = MagicMock()
    broken.table.side_effect = RuntimeError("pool down")
    with patch.object(hb, "get_supabase", return_value=broken):
        assert hb._pool_jk("u1", "PM", "Acme") is None


def test_an_applied_indeed_posting_closes_its_hand_back_whatever_the_spelling():
    """The hand-back holds /viewjob?jk=; the application may be saved under ?vjk=."""
    client, calls, tbl = _fake_supabase()
    open_rows = [
        {"id": "h1", "url": "https://www.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b"},
        {"id": "h2", "url": "https://www.indeed.com/viewjob?jk=9999999999999999"},
        {"id": "h3", "url": _STEP_REVIEW},
    ]
    tbl.in_ = MagicMock(return_value=tbl)
    with (
        patch.object(hb, "get_supabase", return_value=client),
        patch.object(hb, "fetch_paged", return_value=open_rows),
    ):
        n = hb.resolve_for_posting("u1", "https://www.indeed.com/jobs?q=pm&vjk=1a2b3c4d5e6f7a8b")
    assert n == 1
    tbl.in_.assert_called_once_with("id", ["h1"])
