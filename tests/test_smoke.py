"""Smoke tests for app/main.py — must stay green after every refactoring PR.

These don't test business logic. They verify that:
1. The app boots.
2. Public health endpoint works.
3. Auth dependency rejects unauthenticated requests.
4. With a mocked Supabase + auth, key endpoints return structured responses.

Failure of any of these = regression in prod flow.
"""


def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body == {"status": "ok", "version": "1.0.0"}


def test_profile_requires_auth_header(client):
    res = client.get("/api/v1/profile")
    assert res.status_code in (401, 422)


def test_profile_invalid_bearer_format(client):
    res = client.get("/api/v1/profile", headers={"Authorization": "NotBearer xxx"})
    assert res.status_code == 401


def test_profile_with_mock_user_returns_defaults(auth_client, supabase_mock):
    supabase_mock.table.return_value.select.return_value \
        .eq.return_value.execute.return_value.data = []

    res = auth_client.get("/api/v1/profile")
    assert res.status_code == 200
    body = res.json()
    assert "name" in body
    assert "platforms" in body


def test_stats_with_mock_user(auth_client, supabase_mock):
    count_chain = supabase_mock.table.return_value.select.return_value.eq.return_value
    count_chain.execute.return_value.count = 0
    count_chain.gte.return_value.execute.return_value.count = 0

    res = auth_client.get("/api/v1/stats")
    assert res.status_code == 200
    body = res.json()
    assert "total_jobs" in body
    assert "total_applications" in body
    assert "new_today" in body


def test_applications_history_returns_list(auth_client, supabase_mock):
    supabase_mock.table.return_value.select.return_value.eq.return_value \
        .order.return_value.limit.return_value.execute.return_value.data = []

    res = auth_client.get("/api/v1/applications/history")
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_campaign_status_with_mock_user(auth_client, supabase_mock):
    state_chain = supabase_mock.table.return_value.select.return_value.eq.return_value
    state_chain.execute.return_value.data = []
    state_chain.execute.return_value.count = 0

    res = auth_client.get("/api/v1/campaign/status")
    assert res.status_code == 200
    body = res.json()
    assert "running" in body
    assert "today_applications" in body
