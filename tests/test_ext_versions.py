"""The fleet's extension-version spread must be a query, not archaeology.

09-23: a user's "bug" (the red 'Campaign stopped' card on every run) was the store
extension at 1.8.3 while the repo was at 1.8.15 — and the only evidence was the
'resuming (ext 1.8.3)' substring in her activity log. Two guarantees under test:

  * /extension/ping persists the reported version, but only when it CHANGED —
    the steady state (a ping a minute per user) must add zero writes;
  * /tools/ext-versions aggregates the spread for admins, and only admins.
"""

from unittest.mock import patch

from app.routers.tools import ext_versions


def _ping(auth_client, version=None):
    body = {"campaign_running": False}
    if version is not None:
        body["version"] = version
    with patch("app.routers.campaign.campaign_db.reconcile_not_running"):
        return auth_client.post("/api/v1/extension/ping", json=body)


def test_new_version_is_recorded(auth_client):
    with (
        patch(
            "app.routers.campaign.campaign_db.get_state",
            return_value={"running": False, "ext_version": "1.8.3"},
        ),
        patch("app.routers.campaign.campaign_db.record_ext_version") as record,
    ):
        res = _ping(auth_client, version="1.8.15")

    assert res.status_code == 200
    record.assert_called_once()
    assert record.call_args[0][1] == "1.8.15"


def test_unchanged_version_writes_nothing(auth_client):
    """THE cost guard: a ping a minute per user must not become a write a minute."""
    with (
        patch(
            "app.routers.campaign.campaign_db.get_state",
            return_value={"running": False, "ext_version": "1.8.15"},
        ),
        patch("app.routers.campaign.campaign_db.record_ext_version") as record,
    ):
        res = _ping(auth_client, version="1.8.15")

    assert res.status_code == 200
    record.assert_not_called()


def test_versionless_ping_writes_nothing(auth_client):
    with (
        patch(
            "app.routers.campaign.campaign_db.get_state",
            return_value={"running": False, "ext_version": None},
        ),
        patch("app.routers.campaign.campaign_db.record_ext_version") as record,
    ):
        res = _ping(auth_client, version=None)

    assert res.status_code == 200
    record.assert_not_called()


def test_state_read_failure_still_records(auth_client):
    """A state-read hiccup must not silently drop the version — write anyway (the
    write is best-effort on its own), and the ping still fail-opens should_run."""
    with (
        patch(
            "app.routers.campaign.campaign_db.get_state",
            side_effect=RuntimeError("supabase down"),
        ),
        patch("app.routers.campaign.campaign_db.record_ext_version") as record,
    ):
        res = _ping(auth_client, version="1.8.15")

    assert res.status_code == 200
    assert res.json()["should_run"] is True
    record.assert_called_once()


def test_ext_versions_is_admin_only(auth_client):
    res = auth_client.get("/api/v1/tools/ext-versions")
    assert res.status_code == 403


def test_ext_versions_aggregates_the_spread(auth_client):
    rows = [
        {
            "user_id": "u1",
            "ext_version": "1.8.3",
            "ext_version_at": "2026-09-23T10:00:00+00:00",
            "last_ping_at": None,
            "running": False,
        },
        {
            "user_id": "u2",
            "ext_version": "1.8.3",
            "ext_version_at": "2026-09-23T12:00:00+00:00",
            "last_ping_at": None,
            "running": True,
        },
        {
            "user_id": "u3",
            "ext_version": "1.8.15",
            "ext_version_at": "2026-09-23T11:00:00+00:00",
            "last_ping_at": None,
            "running": False,
        },
        {
            "user_id": "u4",
            "ext_version": None,
            "ext_version_at": None,
            "last_ping_at": None,
            "running": False,
        },
    ]
    with (
        patch("app.routers.tools.is_admin", return_value=True),
        patch("app.db.client.fetch_paged", return_value=rows),
    ):

        class _Admin:
            id = "admin"
            email = "admin@example.com"

        out = ext_versions(user=_Admin())

    assert out["latest_in_repo"]  # read from the real manifest — some version string
    assert out["versions"]["1.8.3"] == {
        "users": 2,
        "running_now": 1,
        "last_seen": "2026-09-23T12:00:00+00:00",
    }
    assert out["versions"]["1.8.15"]["users"] == 1
    assert out["users_unknown"] == 1
    # u1/u2 lag whatever the repo's latest is; 1.8.15 counts only if it, too, is behind.
    assert out["users_behind"] >= 2
    # Numeric ordering, newest first, unknown last: 1.8.15 before 1.8.3 (lexicographic
    # ordering would invert them), unknown at the end.
    assert list(out["versions"]) == ["1.8.15", "1.8.3", "unknown"]
