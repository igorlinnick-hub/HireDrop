"""The admin board's partner ledger.

One promise worth pinning: every partner row carries its whole link. The board's
approval result disappears once you close it (and, until the board fix of
2026-09-28, the moment you pressed Approve), so the ledger is the one place a
partner's "what was my link again?" is answered without reconstructing it.
"""

from unittest.mock import MagicMock, patch

from app.routers import admin
from config import FRONTEND_URL

ROWS = {
    "affiliates": [
        {
            "id": "aff_1",
            "code": "igor",
            "status": "active",
            "commission_pct": 30,
            "paypal_email": None,
        }
    ],
    "referrals": [],
    "commissions": [],
    "payouts": [],
    "affiliate_applications": [],
}


def _section():
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value.data = []
    with (
        patch("app.routers.admin._rows", side_effect=lambda table, *a, **k: ROWS[table]),
        patch("app.routers.admin.get_supabase", return_value=sb),
    ):
        return admin._section_affiliates("2026-09-01T00:00:00Z", "2026-09-30T00:00:00Z")


def _partners(section):
    return next(t for t in section["tables"] if t["key"] == "partners")


def test_every_partner_row_carries_its_whole_link():
    table = _partners(_section())
    assert "link" in [c["key"] for c in table["columns"]]
    (row,) = table["rows"]
    assert row["link"] == f"{FRONTEND_URL}/?ref=igor"


def test_the_link_is_built_from_the_site_url_not_guessed():
    """FRONTEND_URL is the one source of the domain. A board that assembled
    hiredrop.io itself would hand out dead links from any other environment."""
    row = _partners(_section())["rows"][0]
    assert row["link"].startswith(FRONTEND_URL)


def test_payable_excludes_a_commission_already_claimed_by_an_in_flight_connect_payout():
    """scripts/run_affiliate_payouts.py sets commissions.payout_id the moment it
    bundles a commission into a Stripe transfer — BEFORE Stripe confirms, so the
    row is still 'accrued'. If the board still called that money "payable" it
    would invite sending it a second time by hand (blast-radius review,
    2026-10-01). "owed" is a lifetime liability and should still count it;
    "payable" must not."""
    rows = {
        "affiliates": [
            {
                "id": "aff_1",
                "code": "igor",
                "status": "active",
                "commission_pct": 30,
                "paypal_email": None,
            }
        ],
        "referrals": [],
        "commissions": [
            {
                "affiliate_id": "aff_1",
                "amount_cents": 1000,
                "status": "accrued",
                "created_at": "2026-01-01T00:00:00Z",
                "payout_id": None,  # genuinely unclaimed
            },
            {
                "affiliate_id": "aff_1",
                "amount_cents": 2000,
                "status": "accrued",
                "created_at": "2026-01-01T00:00:00Z",
                "payout_id": "payout_pending_1",  # claimed by an in-flight Connect payout
            },
        ],
        "payouts": [],
        "affiliate_applications": [],
    }
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value.data = []
    with (
        patch("app.routers.admin._rows", side_effect=lambda table, *a, **k: rows[table]),
        patch("app.routers.admin.get_supabase", return_value=sb),
    ):
        section = admin._section_affiliates("2026-01-01T00:00:00Z", "2026-12-31T00:00:00Z")

    row = _partners(section)["rows"][0]
    assert row["owed"] == 30.0  # both commissions are a real liability ($10 + $20)
    assert row["payable"] == 10.0  # only the unclaimed one is safe to send by hand
