"""Endpoint coverage tests for jobs, extension, and tools routers."""

from unittest.mock import patch


def test_get_jobs_returns_list(auth_client):
    with patch("app.routers.jobs.jobs_db.get_jobs", return_value=[]):
        res = auth_client.get("/api/v1/jobs")
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_patch_job_status(auth_client):
    with patch("app.routers.jobs.jobs_db.update_job_status"):
        res = auth_client.patch(
            "/api/v1/jobs/abc123/status",
            json={"status": "applied"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["updated"] is True
    assert body["job_id"] == "abc123"


def test_get_selectors_not_found(auth_client):
    with patch("app.routers.extension.selectors_db.get", return_value=None):
        res = auth_client.get("/api/v1/extension/selectors/indeed")
    assert res.status_code == 404


def test_get_selectors_found(auth_client):
    fake_row = {
        "platform": "indeed",
        "version": "1.0",
        "selectors_json": {"jobCards": [".job"]},
        "updated_at": "2026-01-01T00:00:00Z",
    }
    with patch("app.routers.extension.selectors_db.get", return_value=fake_row):
        res = auth_client.get("/api/v1/extension/selectors/indeed")
    assert res.status_code == 200
    assert res.json()["platform"] == "indeed"


def test_patch_selectors_non_admin_forbidden(auth_client):
    res = auth_client.patch(
        "/api/v1/extension/selectors/indeed/jobCards",
        json={},
    )
    assert res.status_code == 403


def test_checklist_returns_keys(auth_client):
    with (
        patch(
            "app.routers.tools.get_profile",
            return_value={
                "keywords": ["python"],
                "platforms": ["indeed"],
                "resume_url": "https://example.com/r.pdf",
            },
        ),
        patch("app.routers.tools.jobs_db.count_jobs", return_value=5),
    ):
        res = auth_client.get("/api/v1/checklist")
    assert res.status_code == 200
    body = res.json()
    assert "resume" in body
    assert "keywords" in body
    assert "complete" in body


def test_platform_inbox_urls(auth_client):
    with patch("app.routers.tools.get_profile", return_value={"platforms": ["indeed"]}):
        res = auth_client.get("/api/v1/platform/inbox-urls")
    assert res.status_code == 200


def test_profile_returns_fields(auth_client):
    fake_profile = {
        "name": "Igor",
        "last_name": "Linnik",
        "email": "igor@test.com",
        "phone": "555-1234",
        "keywords": ["python"],
        "platforms": ["indeed"],
        "location": "remote",
        "writing_style": "",
        "resume_url": None,
    }
    with patch("app.routers.profile.profile_db.get_profile", return_value=fake_profile):
        res = auth_client.get("/api/v1/profile")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "Igor"
    assert "platforms" in body


def test_cover_letter_template_endpoint_is_gone(auth_client):
    """POST /tools/cover-letter-template was removed 2026-09-24, not just gated.

    It wrote ONE global templates/cover_letter.txt with no user scoping, and
    fallback_template() rendered that text into OTHER users' cover letters on any
    AI outage — cross-tenant stored-content injection. Nothing called it (no
    dashboard, no extension), so the fallback template is repo-owned now.
    """
    res = auth_client.post(
        "/api/v1/tools/cover-letter-template",
        json={"template": "Dear {company}, applying for {title}. {name}"},
    )
    assert res.status_code == 404


def test_cover_letter_endpoint(auth_client):
    fake_job = {"title": "Backend Dev", "company": "Acme", "description": "Python role"}
    with (
        patch("app.routers.tools.jobs_db.get_job_by_id", return_value=fake_job),
        patch("app.routers.tools.generate_cover_letter", return_value="Dear Acme..."),
        patch("app.routers.tools.usage_db.increment_today"),
        patch("app.routers.tools.usage_db.get_today_count", return_value=0),
    ):
        res = auth_client.post("/api/v1/tools/cover-letter", json={"job_id": "abc"})
    assert res.status_code == 200
    assert "letter" in res.json()


def test_cover_letter_job_not_found(auth_client):
    with (
        patch("app.routers.tools.jobs_db.get_job_by_id", return_value=None),
        patch("app.routers.tools.usage_db.get_today_count", return_value=0),
    ):
        res = auth_client.post("/api/v1/tools/cover-letter", json={"job_id": "missing"})
    assert res.status_code == 404


def test_post_profile_saves(auth_client):
    with patch("app.routers.profile.profile_db.update_profile", return_value={"name": "Igor"}):
        res = auth_client.post(
            "/api/v1/profile",
            json={
                "name": "Igor",
                "last_name": "L",
                "email": "i@test.com",
                "phone": "",
                "keywords": [],
                "location": "remote",
                "job_type": "full-time",
                "platforms": [],
                "writing_style": "",
            },
        )
    assert res.status_code == 200
    assert res.json()["message"] == "Profile saved"


def test_post_profile_partial_save_keeps_current_employment(auth_client):
    """A save that doesn't send current_employer/current_title must not write them —
    the _OPTIONAL_PROFILE_FIELDS rule: a partial save (another Settings card) must
    never blank what the user entered elsewhere."""
    with patch("app.routers.profile.profile_db.update_profile", return_value={}) as upd:
        res = auth_client.post("/api/v1/profile", json={"name": "Igor"})
    assert res.status_code == 200
    payload = upd.call_args.args[1]
    assert "current_employer" not in payload
    assert "current_title" not in payload


def test_post_profile_saves_current_employment_when_sent(auth_client):
    with patch("app.routers.profile.profile_db.update_profile", return_value={}) as upd:
        res = auth_client.post(
            "/api/v1/profile",
            json={"name": "Igor", "current_employer": "Acme Corp", "current_title": "Engineer"},
        )
    assert res.status_code == 200
    payload = upd.call_args.args[1]
    assert payload["current_employer"] == "Acme Corp"
    assert payload["current_title"] == "Engineer"


def test_post_profile_prefs(auth_client):
    fake_profile = {
        "name": "Igor",
        "last_name": "L",
        "phone": "",
        "writing_style": "",
        "resume_url": None,
    }
    with (
        patch("app.routers.profile.profile_db.get_profile", return_value=fake_profile),
        patch("app.routers.profile.profile_db.update_profile", return_value={}),
    ):
        res = auth_client.post(
            "/api/v1/profile/prefs",
            json={
                "keywords": ["python"],
                "location": "remote",
                "job_type": "full-time",
                "platforms": ["indeed"],
            },
        )
    assert res.status_code == 200
    assert res.json()["saved"] is True


def test_resume_url_not_found(auth_client):
    with patch("app.routers.profile.resume_storage.signed_download_url", return_value=None):
        res = auth_client.get("/api/v1/profile/resume/url")
    assert res.status_code == 404


def test_resume_url_found(auth_client):
    with patch(
        "app.routers.profile.resume_storage.signed_download_url",
        return_value="https://example.com/resume.pdf",
    ):
        with patch("app.routers.profile.resume_storage.SIGNED_URL_TTL_SECONDS", 3600):
            res = auth_client.get("/api/v1/profile/resume/url")
    assert res.status_code == 200
    assert "url" in res.json()


def test_resume_status(auth_client):
    with patch("app.routers.profile.resume_storage.exists", return_value=False):
        res = auth_client.get("/api/v1/profile/resume/status")
    assert res.status_code == 200
    assert res.json()["uploaded"] is False


# ── Manual application status (PATCH /applications/{id}/status) ──────────────
# The user's own "the employer answered" channel. The automatic email parser is
# off (one shared inbox, matched by company across all users), so this is the
# only way a row leaves "applied".


def test_patch_application_status_ok(auth_client):
    with patch("app.routers.applications.apps_db.update_status", return_value=True) as upd:
        res = auth_client.patch(
            "/api/v1/applications/app-1/status",
            json={"status": "interview"},
        )
    assert res.status_code == 200
    assert res.json() == {"id": "app-1", "status": "interview"}
    # The owner's id must reach the DB layer — that filter is the IDOR defense.
    assert upd.call_args.args[:2] == ("app-1", "interview")
    assert upd.call_args.args[2]


def test_patch_application_status_normalizes_case(auth_client):
    with patch("app.routers.applications.apps_db.update_status", return_value=True) as upd:
        res = auth_client.patch(
            "/api/v1/applications/app-1/status",
            json={"status": "  Interview  "},
        )
    assert res.status_code == 200
    assert upd.call_args.args[1] == "interview"


def test_patch_application_status_rejects_executor_written_state(auth_client):
    """`applied_unconfirmed` is the executor's honesty signal, not a user's to write."""
    with patch("app.routers.applications.apps_db.update_status") as upd:
        res = auth_client.patch(
            "/api/v1/applications/app-1/status",
            json={"status": "applied_unconfirmed"},
        )
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_status"
    upd.assert_not_called()


def test_patch_application_status_not_found(auth_client):
    """A row owned by someone else matches nothing and reads as 404."""
    with patch("app.routers.applications.apps_db.update_status", return_value=False):
        res = auth_client.patch(
            "/api/v1/applications/someone-elses/status",
            json={"status": "rejected"},
        )
    assert res.status_code == 404


def test_email_surfaces_are_gone(auth_client):
    """The shared-inbox endpoints were removed 2026-09-20, not just disabled.

    /tools/email-check returned the contents of ONE shared mailbox to any signed-in
    user, and /email/status-updates was the vestige of the same idea. Marking a reply
    is the owner's job now (PATCH /applications/{id}/status).
    """
    assert auth_client.get("/api/v1/tools/email-check").status_code == 404
    assert auth_client.get("/api/v1/email/status-updates").status_code == 404


# ── POST /jobs/describe — the posting text the extension is reading ──────────
# The server can never fetch an Indeed page (403), so this is the ONLY channel by
# which a real Indeed description reaches the row that tailoring, the fit judge and
# the interview kit all read.

_POSTING = "Own delivery across three teams. " * 12  # ~400 chars


def test_describe_job_stores_the_posting(auth_client):
    with patch("app.routers.jobs.jobs_db.save_description", return_value="job-9") as save:
        res = auth_client.post(
            "/api/v1/jobs/describe",
            json={
                "link": "https://www.indeed.com/viewjob?jk=abc",
                "description": _POSTING,
                "title": "PM",
                "company": "Corvant",
                "platform": "indeed",
            },
        )
    assert res.status_code == 200
    assert res.json()["stored"] is True
    assert save.call_args.args[1] == "https://www.indeed.com/viewjob?jk=abc"


def test_describe_job_refuses_a_search_snippet(auth_client):
    """ "From $40,000 a yearFull-time" must never overwrite real harvested text."""
    with patch("app.routers.jobs.jobs_db.save_description") as save:
        res = auth_client.post(
            "/api/v1/jobs/describe",
            json={"link": "https://x/1", "description": "From $40,000 a yearFull-time"},
        )
    assert res.status_code == 200
    assert res.json()["stored"] is False
    save.assert_not_called()


def test_describe_job_caps_a_page_dump(auth_client):
    with patch("app.routers.jobs.jobs_db.save_description", return_value="job-9") as save:
        res = auth_client.post(
            "/api/v1/jobs/describe",
            json={"link": "https://x/1", "description": "y" * 50_000},
        )
    assert res.status_code == 200
    assert res.json()["chars"] == 20_000
    assert len(save.call_args.args[2]) == 20_000


def test_interview_kit_refuses_to_prep_from_a_snippet(auth_client):
    """A salary string is truthy — it used to buy a full prep sheet built on nothing."""
    thin = {
        "id": "a1",
        "title": "Event Manager",
        "company": "SchooLinks",
        "platform": "indeed",
        "link": "https://x/1",
        "description": "From $40,000 a yearFull-time",
        "location": "",
        "status": "applied",
    }
    with (
        patch("app.routers.applications.apps_db.get_for_interview_kit", return_value=thin),
        patch("app.routers.applications.kit_db.get_kit", return_value=None),
    ):
        res = auth_client.get("/api/v1/applications/a1/interview-kit")
        assert res.status_code == 200
        assert res.json()["can_generate"] is False

        post = auth_client.post("/api/v1/applications/a1/interview-kit")
        assert post.status_code == 422
        assert post.json()["error"] == "no_job_text"
