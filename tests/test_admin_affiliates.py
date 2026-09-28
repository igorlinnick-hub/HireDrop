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
