"""Hand-back telemetry: the operator-only aggregation of what the filler can't submit.

Two things must hold, and they are the whole point of the feature:
  1. It aggregates ONLY rows the extension tagged `type: "handback"` — the warn level
     is shared with cap hits and save failures, so level alone would inflate counts.
  2. It is admin-gated. It reads across users by design; leaking it to a job seeker
     would show them other people's jobs AND our engineering backlog.
"""

from app.db import activity as activity_db


def _rows_chain(supabase_mock):
    """The fluent chain handback_stats() builds: select→eq→gte→order→limit→execute."""
    return (
        supabase_mock.table.return_value.select.return_value.eq.return_value.gte.return_value.order.return_value.limit.return_value.execute.return_value
    )


def test_handback_stats_aggregates_only_tagged_rows(supabase_mock):
    _rows_chain(supabase_mock).data = [
        {
            "timestamp": "2026-08-11T10:00:00Z",
            "user_id": "u1",
            "message": "✋ Needs your hands: A @ X",
            "metadata_json": {
                "type": "handback",
                "platform": "greenhouse",
                "unfilled": ["linkedin profile", "work authorization"],
            },
        },
        {
            "timestamp": "2026-08-11T09:00:00Z",
            "user_id": "u2",
            "message": "✋ Needs your hands: B @ Y",
            "metadata_json": {
                "type": "handback",
                "platform": "greenhouse",
                "unfilled": ["linkedin profile"],
            },
        },
        # A warn line that is NOT a hand-back — must be ignored entirely.
        {
            "timestamp": "2026-08-11T08:00:00Z",
            "user_id": "u1",
            "message": "⚠️ Applied but backend save failed (429)",
            "metadata_json": {},
        },
    ]

    out = activity_db.handback_stats(window_hours=24)

    assert out["handbacks"] == 2
    assert out["affected_users"] == 2
    assert out["by_platform"] == {"greenhouse": 2}
    # Most frequent blocking field first — this ordering IS the fix backlog.
    assert out["top_fields"][0] == {"label": "linkedin profile", "count": 2}


def test_handback_stats_survives_missing_metadata(supabase_mock):
    """Legacy rows predate the metadata write; they must not crash the aggregation."""
    _rows_chain(supabase_mock).data = [
        {"timestamp": "2026-08-11T10:00:00Z", "user_id": "u1", "message": "x", "metadata_json": None},
    ]

    out = activity_db.handback_stats()

    assert out["handbacks"] == 0
    assert out["top_fields"] == []


def test_handbacks_endpoint_is_admin_only(auth_client):
    """FakeUser is not in ADMIN_EMAILS — a normal user gets 403, never other users' data."""
    res = auth_client.get("/api/v1/activity/handbacks")

    assert res.status_code == 403
    assert res.json()["error"] == "Admin only"
