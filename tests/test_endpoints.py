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


def test_cover_letter_template_saves(auth_client):
    from unittest.mock import mock_open

    m = mock_open()
    with (
        patch("app.routers.tools.os.makedirs"),
        patch("builtins.open", m),
    ):
        res = auth_client.post(
            "/api/v1/tools/cover-letter-template",
            json={"template": "Dear {company}, applying for {title}. {name}"},
        )
    assert res.status_code == 200
    assert res.json()["saved"] is True


def test_email_check_not_configured(auth_client):
    with (
        patch("config.EMAIL_ADDRESS", ""),
        patch("config.EMAIL_PASSWORD", ""),
    ):
        res = auth_client.get("/api/v1/tools/email-check")
    assert res.status_code == 200
    assert res.json()["configured"] is False


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


def test_email_status_updates(auth_client):
    interview_app = {
        "id": "a1",
        "title": "Dev",
        "company": "Acme",
        "platform": "indeed",
        "link": "https://indeed.com/1",
        "date_applied": "2026-06-16",
        "status": "interview",
        "cover_letter": "",
    }
    with patch("app.routers.email_processor.apps_db.get_history", return_value=[interview_app]):
        res = auth_client.get("/api/v1/email/status-updates")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["status"] == "interview"
