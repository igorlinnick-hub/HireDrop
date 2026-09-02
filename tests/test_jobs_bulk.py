"""save_jobs_bulk / existing_links — the one-round-trip write path for discovery
and harvest. Guards the three behaviors the batch layer promises: in-batch link
dedup (Postgres can't touch a row twice in one upsert), graceful downgrade when
the batch write fails, and chunked IN-queries for the dedup read."""

from unittest.mock import MagicMock, patch

from app.db import jobs as jobs_db


def _job(link, score=None):
    return {"title": "t", "company": "c", "link": link, "score": score}


def test_bulk_upserts_once_and_dedupes_links():
    fake = MagicMock()
    with patch("app.db.jobs.get_supabase", return_value=fake):
        saved = jobs_db.save_jobs_bulk("u1", [_job("a"), _job("b"), _job("a")])

    assert saved == 2
    upsert = fake.table.return_value.upsert
    assert upsert.call_count == 1
    rows = upsert.call_args.args[0]
    assert [r["link"] for r in rows] == ["a", "b"]
    # Uniform keys: score fields present even when unscored (PostgREST bulk rule).
    assert all("score" in r for r in rows)


def test_bulk_empty_batch_writes_nothing():
    fake = MagicMock()
    with patch("app.db.jobs.get_supabase", return_value=fake):
        assert jobs_db.save_jobs_bulk("u1", []) == 0
    fake.table.return_value.upsert.assert_not_called()


def test_bulk_downgrades_to_per_row_on_batch_failure():
    fake = MagicMock()
    execute = fake.table.return_value.upsert.return_value.execute
    # Both batch attempts (scored, then core columns) fail; per-row save_job succeeds.
    row_result = MagicMock()
    row_result.data = [{"id": "job-1"}]
    execute.side_effect = [RuntimeError("no column"), RuntimeError("no column")] + [row_result] * 2

    with patch("app.db.jobs.get_supabase", return_value=fake):
        saved = jobs_db.save_jobs_bulk("u1", [_job("a", score=80), _job("b")])

    assert saved == 2
    # 2 failed batch attempts + 2 per-row upserts.
    assert fake.table.return_value.upsert.call_count == 4


def test_existing_links_chunks_the_in_query():
    fake = MagicMock()
    chain = fake.table.return_value.select.return_value.eq.return_value
    chain.in_.return_value.execute.return_value.data = [{"link": "seen"}]

    with patch("app.db.jobs.get_supabase", return_value=fake):
        out = jobs_db.existing_links("u1", [f"link-{i}" for i in range(85)])

    assert out == {"seen"}
    assert chain.in_.call_count == 3  # 40 + 40 + 5
    first_chunk = chain.in_.call_args_list[0].args[1]
    assert len(first_chunk) == 40
