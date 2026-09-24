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


# ------------------------------------------------------- the approval email


@pytest.fixture
def mail():
    """Patch the sender; returns the list of (to, subject, html) it was given."""
    sent: list = []

    def fake(to, subject, html):
        sent.append((to, subject, html))
        return True

    with patch("app.routers.affiliate.send_email", side_effect=fake):
        yield sent


def test_approval_emails_the_partner_their_link(client, db, admin_env, mail):
    db.tables["affiliate_applications"] = [
        {
            "id": "app_1",
            "status": "new",
            "desired_code": "lauren",
            "email": "lauren@uni.edu",
            "name": "Lauren Diaz",
        }
    ]
    with patch("app.routers.affiliate._user_id_for_email", return_value="user_1"):
        res = client.post(
            f"{API}/admin/affiliates/decide",
            json={"application_id": "app_1", "approve": True},
            headers=ADMIN,
        )
    assert res.status_code == 200, res.text
    assert res.json()["emailed"] is True
    (to, subject, html) = mail[0]
    assert to == "lauren@uni.edu"
    assert "hiredrop.io/?ref=lauren" in html or "/?ref=lauren" in html
    # Account exists, so the link is live — no invite, and nothing that tells
    # them to go create an account they already have.
    assert "signup?affiliate" not in html


def test_a_reserved_code_is_emailed_the_invite_not_the_dashboard(client, db, admin_env, mail):
    """Without an account the link earns nothing until they sign up with THAT
    address — so that is the one thing the email has to lead with."""
    db.tables["affiliate_applications"] = [
        {
            "id": "app_1",
            "status": "new",
            "desired_code": "newbie",
            "email": "new@uni.edu",
            "name": "",
        }
    ]
    with patch("app.routers.affiliate._user_id_for_email", return_value=None):
        res = client.post(
            f"{API}/admin/affiliates/decide",
            json={"application_id": "app_1", "approve": True},
            headers=ADMIN,
        )
    assert res.json()["live_now"] is False
    html = mail[0][2]
    assert "affiliate=newbie" in html and "email=new%40uni.edu" in html


def test_rejection_sends_nothing(client, db, admin_env, mail):
    """Silence is deliberate: a rejection is a conversation, not a notification."""
    db.tables["affiliate_applications"] = [
        {"id": "app_1", "status": "new", "desired_code": "nope", "email": "n@uni.edu", "name": "N"}
    ]
    res = client.post(
        f"{API}/admin/affiliates/decide",
        json={"application_id": "app_1", "approve": False},
        headers=ADMIN,
    )
    assert res.status_code == 200
    assert mail == []


def test_a_failed_send_does_not_undo_the_approval(client, db, admin_env):
    """The link is already real when the email goes out. Losing the email must
    cost the partner a resend, not their code."""
    db.tables["affiliate_applications"] = [
        {
            "id": "app_1",
            "status": "new",
            "desired_code": "lauren",
            "email": "lauren@uni.edu",
            "name": "Lauren",
        }
    ]
    with (
        patch("app.routers.affiliate._user_id_for_email", return_value="user_1"),
        patch("app.routers.affiliate.send_email", return_value=False),
    ):
        res = client.post(
            f"{API}/admin/affiliates/decide",
            json={"application_id": "app_1", "approve": True},
            headers=ADMIN,
        )
    assert res.status_code == 200
    assert res.json()["emailed"] is False
    assert inserts(db.log, "affiliates")  # the affiliate row still exists


def test_resend_needs_an_approved_application(client, db, admin_env, mail):
    res = client.post(f"{API}/admin/affiliates/resend", json={"code": "ghost"}, headers=ADMIN)
    assert res.status_code == 404
    assert mail == []


# --------------------------------------------- the code must land on an account


def test_a_signed_in_applicant_cannot_apply_as_someone_else(client, db):
    """The browser marks the email read-only; that is a courtesy. This is the
    rule: an approved code attaches to an account BY EMAIL, so a body claiming
    a different address would produce an approved partner whose link belongs to
    no account — and no screen they can reach would ever say so."""
    with patch("app.routers.affiliate._email_of_caller", return_value="lauren@uni.edu"):
        res = client.post(
            f"{API}/affiliate/apply",
            json={
                "name": "Lauren",
                "email": "someone-else@evil.com",
                "desired_code": "lauren",
            },
            headers={"Authorization": "Bearer jwt-of-lauren"},
        )
    assert res.status_code == 200, res.text
    (row,) = inserts(db.log, "affiliate_applications")
    assert row["email"] == "lauren@uni.edu"


def test_a_stranger_still_applies_with_the_address_they_typed(client, db):
    """No token is not an error — the form is reachable signed out, and that
    path stays open (the invite email pins the address instead)."""
    res = client.post(
        f"{API}/affiliate/apply",
        json={"name": "Luca", "email": "Luca@Uni.edu", "desired_code": "luca"},
    )
    assert res.status_code == 200, res.text
    (row,) = inserts(db.log, "affiliate_applications")
    assert row["email"] == "luca@uni.edu"  # normalised, as the column requires


def test_a_broken_token_is_treated_as_a_stranger_not_an_error(client, db):
    """An expired session must not turn applying into a 401 dead end."""
    with patch("app.routers.affiliate.get_supabase") as sb:
        sb.return_value.auth.get_user.side_effect = RuntimeError("expired")
        assert mod._email_of_caller("Bearer stale-jwt") is None
    res = client.post(
        f"{API}/affiliate/apply",
        json={"name": "Luca", "email": "luca@uni.edu", "desired_code": "luca2"},
        headers={"Authorization": "Bearer stale-jwt"},
    )
    assert res.status_code == 200


def test_an_extension_key_is_never_the_author_of_a_web_form(client, db):
    assert mod._email_of_caller("Bearer hd_something") is None
    assert mod._email_of_caller(None) is None
    assert mod._email_of_caller("Basic abc") is None


def test_issuing_uses_the_body_email_not_the_admins(client, db, admin_env):
    """The admin is issuing a link FOR someone. Binding it to the caller — the
    rule that is right on /affiliate/apply — would hand every partner's link to
    whoever pressed the button."""
    with patch("app.routers.affiliate._user_id_for_email", return_value=None):
        res = client.post(
            f"{API}/admin/affiliates/issue",
            json={"email": "lauren@uni.edu", "code": "lauren"},
            headers={**ADMIN, "Authorization": "Bearer igors-own-jwt"},
        )
    assert res.status_code == 200, res.text
    (app_row,) = inserts(db.log, "affiliate_applications")
    assert app_row["email"] == "lauren@uni.edu"
