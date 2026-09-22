"""The two new admin/public surfaces: counting link opens, and issuing a link.

What is worth asserting here is not the happy path (a row goes in) but the
promises the rest of the system leans on:
  * a click never stores a raw IP, and never tells a stranger a code exists
  * issuing a link cannot silently collide with an application already waiting
  * no account yet is not an error — it is a reservation plus an invite URL
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.routers import affiliate as mod

API = "/api/v1"
ADMIN = {"X-Admin-Token": "test-admin-token"}


class FakeTable:
    """Chainable PostgREST stand-in. Reads return `rows`; writes land in `log`."""

    def __init__(self, name, tables, log):
        self.name, self._tables, self.log = name, tables, log
        self._raise_on_insert = tables.get(f"{name}!raise")

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def neq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def insert(self, row, **k):
        if self._raise_on_insert:
            raise RuntimeError(self._raise_on_insert)
        self.log.append(("insert", self.name, row))
        return self

    def update(self, patch_, **k):
        self.log.append(("update", self.name, patch_))
        return self

    def execute(self):
        if self.log and self.log[-1][0] == "insert" and self.log[-1][1] == self.name:
            # Inserts read back their row: the code uses .data[0]["id"].
            return SimpleNamespace(data=[{"id": f"{self.name}_new", **self.log[-1][2]}])
        return SimpleNamespace(data=self._tables.get(self.name, []))


@pytest.fixture
def db():
    """Patch the router's Supabase and clear the in-process rate limiter."""
    tables: dict = {}
    log: list = []
    fake = SimpleNamespace(table=lambda name: FakeTable(name, tables, log))
    mod._attempts.clear()
    with patch("app.routers.affiliate.get_supabase", return_value=fake):
        yield SimpleNamespace(tables=tables, log=log)


def inserts(log, table):
    return [row for op, name, row in log if op == "insert" and name == table]


@pytest.fixture
def admin_env():
    with patch.dict("os.environ", {"ADMIN_METRICS_TOKEN": "test-admin-token"}):
        yield


# ---------------------------------------------------------------- link opens


def test_click_stores_a_hash_and_never_the_address(client, db):
    res = client.post(
        f"{API}/affiliate/click",
        json={"code": "lauren", "landing_page": "/"},
        headers={"X-Forwarded-For": "203.0.113.9", "User-Agent": "Safari"},
    )
    assert res.status_code == 200
    (row,) = inserts(db.log, "affiliate_clicks")
    assert row["code"] == "lauren"
    assert len(row["visitor_hash"]) == 64
    assert "203.0.113.9" not in str(row)


def test_same_visitor_same_day_is_one_open(client, db):
    """The unique index does the deduping; the endpoint must swallow its error
    rather than surface a 500 to a page that has already rendered."""
    db.tables["affiliate_clicks!raise"] = "duplicate key value violates unique constraint"
    res = client.post(
        f"{API}/affiliate/click", json={"code": "lauren"}, headers={"User-Agent": "Safari"}
    )
    assert res.status_code == 200
    assert res.json() == {"status": "counted"}


def test_click_answers_the_same_for_a_junk_code(client, db):
    """Otherwise the endpoint becomes an oracle for which codes exist."""
    res = client.post(f"{API}/affiliate/click", json={"code": "NOT A CODE"})
    assert res.status_code == 200
    assert res.json() == {"status": "counted"}
    assert inserts(db.log, "affiliate_clicks") == []


def test_print_codes_are_counted_like_partner_codes(client, db):
    """?ref=card / ?ref=stka are not affiliates. Counting them is the point:
    they are the only way to tell which printed artefact gets scanned."""
    client.post(f"{API}/affiliate/click", json={"code": "card"}, headers={"User-Agent": "a"})
    assert inserts(db.log, "affiliate_clicks")[0]["code"] == "card"


def test_a_script_hammering_one_ip_stops_being_counted(client, db):
    headers = {"X-Forwarded-For": "198.51.100.7", "User-Agent": "bot"}
    for i in range(mod._MAX_CLICKS_PER_IP + 5):
        client.post(f"{API}/affiliate/click", json={"code": f"c{i}"}, headers=headers)
    assert len(inserts(db.log, "affiliate_clicks")) == mod._MAX_CLICKS_PER_IP


# ------------------------------------------------------------ issuing a link


def test_issue_needs_the_admin_token(client, db, admin_env):
    res = client.post(f"{API}/admin/affiliates/issue", json={"email": "a@b.com", "code": "lauren"})
    assert res.status_code == 401
    assert db.log == []


def test_issue_to_an_existing_account_goes_live_now(client, db, admin_env):
    with patch("app.routers.affiliate._user_id_for_email", return_value="user_1"):
        res = client.post(
            f"{API}/admin/affiliates/issue",
            json={"email": "Lauren@Uni.edu", "code": "Lauren", "name": "Lauren"},
            headers=ADMIN,
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["live_now"] is True
    assert body["code"] == "lauren"  # normalised, like the column's CHECK
    assert body["invite_url"] is None
    assert inserts(db.log, "affiliates")[0]["code"] == "lauren"
    # Paper trail: the board shows every live code and where it came from.
    (app_row,) = inserts(db.log, "affiliate_applications")
    assert app_row["status"] == "approved"
    assert app_row["source"] == "issued-by-admin"


def test_issue_without_an_account_reserves_and_hands_back_an_invite(client, db, admin_env):
    with patch("app.routers.affiliate._user_id_for_email", return_value=None):
        res = client.post(
            f"{API}/admin/affiliates/issue",
            json={"email": "new@uni.edu", "code": "newbie"},
            headers=ADMIN,
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["live_now"] is False
    # The reservation is redeemed by email, so the invite has to carry it.
    assert "email=new%40uni.edu" in body["invite_url"]
    assert "affiliate=newbie" in body["invite_url"]
    assert inserts(db.log, "affiliates") == []
    assert inserts(db.log, "affiliate_applications")[0]["status"] == "approved"


def test_issue_refuses_a_code_that_is_already_a_partner(client, db, admin_env):
    db.tables["affiliates"] = [{"id": "aff_1"}]
    res = client.post(
        f"{API}/admin/affiliates/issue",
        json={"email": "someone@uni.edu", "code": "lauren"},
        headers=ADMIN,
    )
    assert res.status_code == 409
    assert inserts(db.log, "affiliates") == []


def test_issue_refuses_when_that_person_is_already_waiting(client, db, admin_env):
    """Two paths writing the same person is how a partner ends up with two
    links, one of them quietly dead. Decide the application instead."""
    db.tables["affiliate_applications"] = [
        {"id": "app_1", "status": "new", "desired_code": "lauren"}
    ]
    res = client.post(
        f"{API}/admin/affiliates/issue",
        json={"email": "lauren@uni.edu", "code": "lauren2"},
        headers=ADMIN,
    )
    assert res.status_code == 409
    assert "approve that instead" in res.json()["detail"]


def test_issue_refuses_a_reserved_word(client, db, admin_env):
    res = client.post(
        f"{API}/admin/affiliates/issue",
        json={"email": "a@b.com", "code": "support"},
        headers=ADMIN,
    )
    assert res.status_code == 400
