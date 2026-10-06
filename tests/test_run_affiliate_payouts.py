"""scripts/run_affiliate_payouts.py — the daily job that actually moves money.

Proves the three things Igor asked to see before this merges (2026-10-01):
  - a batch's commissions are CLAIMED (payout_id set) before any Stripe call,
    so a second run never re-bundles the same money into a second batch;
  - the Transfer call carries a stable idempotency key keyed on the payout
    row's own id, so resuming a crashed `pending` payout cannot create a
    second transfer — and a failed call leaves it `pending` for retry rather
    than silently losing track of it;
  - resuming a `pending` payout RE-VERIFIES against the commissions actually
    linked to it rather than trusting the row's stored amount_cents — closing
    a crash window a reviewer found between "payout row inserted" and
    "commissions claimed" that could otherwise pay the same money twice.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from scripts import run_affiliate_payouts as job


class FakeTable:
    """Chainable PostgREST stand-in that actually applies `.eq()`/`.in_()`/
    `.is_()`/`.lt()` to the fixture rows, so two different queries against the
    same table (e.g. "pending payouts" vs "a payout's own linked commissions")
    see different, correct results instead of both getting everything. Writes
    are logged with the filters in effect when `.execute()` ran; an insert
    synthesizes an id like Postgres would."""

    def __init__(self, name, rows, log):
        self.name, self._all_rows, self.log = name, rows, log
        self._filters: list[tuple] = []
        self._pending: tuple | None = None

    def select(self, *a, **k):
        return self

    def eq(self, field, value):
        self._filters.append(("eq", field, value))
        return self

    def in_(self, field, values):
        self._filters.append(("in", field, list(values)))
        return self

    def is_(self, field, value):
        self._filters.append(("is", field, value))
        return self

    def lt(self, field, value):
        self._filters.append(("lt", field, value))
        return self

    def update(self, patch_, **k):
        self._pending = ("update", patch_)
        return self

    def insert(self, row, **k):
        self._pending = ("insert", row)
        return self

    def _matching_rows(self):
        rows = self._all_rows
        for kind, field, value in self._filters:
            if kind == "eq":
                rows = [r for r in rows if r.get(field) == value]
            elif kind == "in":
                rows = [r for r in rows if r.get(field) in value]
            elif kind == "is":
                want_null = value is None or value == "null"
                rows = [r for r in rows if (r.get(field) is None) == want_null]
            elif kind == "lt":
                rows = [r for r in rows if (r.get(field) or "") < value]
        return rows

    def execute(self):
        if self._pending:
            op, payload = self._pending
            self.log.append((op, self.name, payload, list(self._filters)))
            if op == "insert":
                return SimpleNamespace(data=[{**payload, "id": f"new_{len(self.log)}"}])
        return SimpleNamespace(data=self._matching_rows())


@pytest.fixture
def fake_db():
    tables: dict[str, list] = {}
    log: list = []
    client = MagicMock()
    client.table.side_effect = lambda name: FakeTable(name, tables.get(name, []), log)
    with patch("scripts.run_affiliate_payouts.get_supabase", return_value=client):
        yield SimpleNamespace(tables=tables, log=log)


@pytest.fixture
def stripe_mock():
    fake = MagicMock()
    with patch("scripts.run_affiliate_payouts._stripe", return_value=fake):
        yield fake


def run_args(**over):
    base = {"dry_run": False, "force": False}
    base.update(over)
    return SimpleNamespace(**base)


AFFILIATE = {
    "id": "aff_1",
    "user_id": "partner",
    "code": "igor",
    "status": "active",
    "stripe_account_id": "acct_123",
    "payouts_enabled": True,
}

OLD = "2020-01-01T00:00:00+00:00"  # safely older than any REFUND_WINDOW_DAYS cutoff


def payout_updates(log):
    return [(p, f) for op, t, p, f in log if t == "payouts" and op == "update"]


def commission_updates(log):
    return [(p, f) for op, t, p, f in log if t == "commissions" and op == "update"]


# ------------------------------------------------------------- resuming pending


def test_resumes_a_pending_payout_and_marks_it_completed(fake_db, stripe_mock):
    fake_db.tables["affiliates"] = [AFFILIATE]
    fake_db.tables["payouts"] = [
        {"id": "payout_1", "affiliate_id": "aff_1", "amount_cents": 3000, "status": "pending"}
    ]
    # Reconciliation reads commissions linked to THIS payout before trusting its
    # stored amount — these actually add up to 3000, so the claim is real.
    fake_db.tables["commissions"] = [
        {
            "id": "c1",
            "affiliate_id": "aff_1",
            "amount_cents": 3000,
            "status": "accrued",
            "payout_id": "payout_1",
        }
    ]
    stripe_mock.Transfer.create.return_value = SimpleNamespace(id="tr_1")

    assert job.cmd_run(run_args()) == 0

    kwargs = stripe_mock.Transfer.create.call_args.kwargs
    assert kwargs["destination"] == "acct_123"
    assert kwargs["amount"] == 3000
    assert kwargs["idempotency_key"] == "affiliate-payout-payout_1"

    (patch_, _filters) = payout_updates(fake_db.log)[0]
    assert patch_["status"] == "completed"
    assert patch_["stripe_transfer_id"] == "tr_1"

    (c_patch, c_filters) = commission_updates(fake_db.log)[0]
    assert c_patch == {"status": "paid_out"}
    assert ("eq", "payout_id", "payout_1") in c_filters


def test_stripe_failure_leaves_the_payout_pending_for_retry(fake_db, stripe_mock):
    fake_db.tables["affiliates"] = [AFFILIATE]
    fake_db.tables["payouts"] = [
        {"id": "payout_1", "affiliate_id": "aff_1", "amount_cents": 3000, "status": "pending"}
    ]
    fake_db.tables["commissions"] = [
        {
            "id": "c1",
            "affiliate_id": "aff_1",
            "amount_cents": 3000,
            "status": "accrued",
            "payout_id": "payout_1",
        }
    ]
    stripe_mock.Transfer.create.side_effect = Exception("stripe is down")

    # Non-zero exit: on Railway cron that's what turns the run red.
    assert job.cmd_run(run_args()) == 1

    assert payout_updates(fake_db.log) == []
    assert commission_updates(fake_db.log) == []


def test_a_pending_payout_claiming_no_real_commissions_is_marked_failed_not_paid(
    fake_db, stripe_mock
):
    """The crash window this closes: insert the payout row, then crash BEFORE
    the claim write sets commissions.payout_id. A next run must not trust the
    row's stored amount_cents and transfer money for commissions that are
    still unclaimed (and about to be picked up as a fresh batch in this same
    run) — that would be the same money sent twice."""
    fake_db.tables["affiliates"] = [AFFILIATE]
    fake_db.tables["payouts"] = [
        {"id": "payout_1", "affiliate_id": "aff_1", "amount_cents": 3000, "status": "pending"}
    ]
    fake_db.tables["commissions"] = []  # nothing actually links to payout_1

    job.cmd_run(run_args())

    stripe_mock.Transfer.create.assert_not_called()
    (patch_, filters) = payout_updates(fake_db.log)[0]
    assert patch_ == {"status": "failed"}
    assert ("eq", "id", "payout_1") in filters


def test_a_pending_payout_with_no_connect_account_is_left_for_manual_settlement(
    fake_db, stripe_mock
):
    fake_db.tables["affiliates"] = [{**AFFILIATE, "stripe_account_id": None}]
    fake_db.tables["payouts"] = [
        {"id": "payout_1", "affiliate_id": "aff_1", "amount_cents": 3000, "status": "pending"}
    ]
    fake_db.tables["commissions"] = []

    job.cmd_run(run_args())

    stripe_mock.Transfer.create.assert_not_called()
    assert payout_updates(fake_db.log) == []


# ----------------------------------------------------------------- new batches


def test_creates_and_settles_a_new_batch_when_the_minimum_is_met(fake_db, stripe_mock):
    fake_db.tables["affiliates"] = [AFFILIATE]
    fake_db.tables["payouts"] = []
    fake_db.tables["commissions"] = [
        {
            "id": "c1",
            "affiliate_id": "aff_1",
            "amount_cents": 1500,
            "status": "accrued",
            "created_at": OLD,
        },
        {
            "id": "c2",
            "affiliate_id": "aff_1",
            "amount_cents": 1200,
            "status": "accrued",
            "created_at": OLD,
        },
    ]
    stripe_mock.Transfer.create.return_value = SimpleNamespace(id="tr_2")

    job.cmd_run(run_args())

    inserts = [p for op, t, p, f in fake_db.log if t == "payouts" and op == "insert"]
    assert inserts[0]["amount_cents"] == 2700
    assert inserts[0]["method"] == "stripe_connect"
    assert inserts[0]["status"] == "pending"

    # The claim (commissions.payout_id set) must happen BEFORE the Stripe call —
    # it is what stops a second run from re-bundling the same commissions.
    claims = [(p, f) for p, f in commission_updates(fake_db.log) if "payout_id" in p]
    assert claims
    assert any(op == "is" and field == "payout_id" for op, field, _val in claims[0][1])

    stripe_mock.Transfer.create.assert_called_once()
    assert stripe_mock.Transfer.create.call_args.kwargs["amount"] == 2700


def test_skips_a_batch_under_the_minimum(fake_db, stripe_mock):
    fake_db.tables["affiliates"] = [AFFILIATE]
    fake_db.tables["payouts"] = []
    fake_db.tables["commissions"] = [
        {
            "id": "c1",
            "affiliate_id": "aff_1",
            "amount_cents": 500,
            "status": "accrued",
            "created_at": OLD,
        }
    ]

    job.cmd_run(run_args())

    assert not any(t == "payouts" for op, t, p, f in fake_db.log)
    stripe_mock.Transfer.create.assert_not_called()


def test_skips_an_affiliate_with_no_connect_account(fake_db, stripe_mock):
    fake_db.tables["affiliates"] = [{**AFFILIATE, "stripe_account_id": None}]
    fake_db.tables["payouts"] = []
    fake_db.tables["commissions"] = [
        {
            "id": "c1",
            "affiliate_id": "aff_1",
            "amount_cents": 5000,
            "status": "accrued",
            "created_at": OLD,
        }
    ]

    job.cmd_run(run_args())

    assert not any(t == "payouts" for op, t, p, f in fake_db.log)
    stripe_mock.Transfer.create.assert_not_called()


def test_skips_an_affiliate_whose_onboarding_is_not_finished(fake_db, stripe_mock):
    fake_db.tables["affiliates"] = [{**AFFILIATE, "payouts_enabled": False}]
    fake_db.tables["payouts"] = []
    fake_db.tables["commissions"] = [
        {
            "id": "c1",
            "affiliate_id": "aff_1",
            "amount_cents": 5000,
            "status": "accrued",
            "created_at": OLD,
        }
    ]

    job.cmd_run(run_args())

    assert not any(t == "payouts" for op, t, p, f in fake_db.log)
    stripe_mock.Transfer.create.assert_not_called()


def test_force_pays_below_the_minimum(fake_db, stripe_mock):
    fake_db.tables["affiliates"] = [AFFILIATE]
    fake_db.tables["payouts"] = []
    fake_db.tables["commissions"] = [
        {
            "id": "c1",
            "affiliate_id": "aff_1",
            "amount_cents": 500,
            "status": "accrued",
            "created_at": OLD,
        }
    ]
    stripe_mock.Transfer.create.return_value = SimpleNamespace(id="tr_3")

    job.cmd_run(run_args(force=True))

    stripe_mock.Transfer.create.assert_called_once()


# -------------------------------------------------------------------- dry run


def test_dry_run_writes_nothing_and_calls_stripe_nothing(fake_db, stripe_mock):
    fake_db.tables["affiliates"] = [AFFILIATE]
    fake_db.tables["payouts"] = [
        {"id": "payout_1", "affiliate_id": "aff_1", "amount_cents": 3000, "status": "pending"}
    ]
    fake_db.tables["commissions"] = [
        {
            "id": "c1",
            "affiliate_id": "aff_1",
            "amount_cents": 5000,
            "status": "accrued",
            "created_at": OLD,
        }
    ]

    job.cmd_run(run_args(dry_run=True))

    assert fake_db.log == []
    stripe_mock.Transfer.create.assert_not_called()


def test_a_run_with_nothing_owed_still_says_it_ran(fake_db, stripe_mock, capsys):
    # A cron job that prints nothing when idle can't be told apart from one
    # that never ran — which is how the missing schedule hid (10-06).
    fake_db.tables["affiliates"] = [AFFILIATE]

    assert job.cmd_run(run_args()) == 0

    assert "payout run done: 0 paid ($0.00), 0 failed" in capsys.readouterr().out
    stripe_mock.Transfer.create.assert_not_called()
