"""scripts/delete_account.py — the account-erasure tool behind the privacy policy.

Pins, against an in-memory Supabase (no network):
  - dry run, every refusal path and a confirm mismatch delete NOTHING;
  - --execute erases storage + rows BEFORE the auth user, and never touches another user;
  - the schema guard fails on a person-pointing column nobody classified;
  - >100 storage files and >1000 rows are all reached despite paging/caps;
  - a referred user's referral is anonymized, not cascaded, when an affiliate earned on it;
  - an empty / partial schema fails closed (exit 3) instead of skipping every check;
  - Stripe is asked by email too, so a customer the profile never recorded still refuses;
  - --user-id: only an explicit 404 means "no auth user", any other auth error stops;
  - the auth user is banned before anything is erased, and late writes are swept.
"""

import sys
from types import SimpleNamespace

import pytest
from supabase_auth.errors import AuthApiError

from scripts import delete_account as da

ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
EMAIL = "me@example.com"
CAP = 1000  # PostgREST max-rows: reads AND (in this fake) each delete stop here


class FakeQuery:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.op, self.filters, self.count, self.patch = "select", [], None, None
        self.lo, self.hi = 0, None

    def select(self, *_cols, count=None, head=None):
        self.op, self.count = "select", count
        return self

    def delete(self, **_kw):
        self.op = "delete"
        return self

    def update(self, patch, **_kw):
        self.op, self.patch = "update", patch
        return self

    def eq(self, col, val):
        self.filters.append((col, [val]))
        return self

    def in_(self, col, vals):
        self.filters.append((col, list(vals)))
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, n):
        self.hi = n - 1
        return self

    def range(self, lo, hi):
        self.lo, self.hi = lo, hi
        return self

    def _match(self):
        rows = self.db.tables.get(self.table, [])
        return [r for r in rows if all(r.get(c) in v for c, v in self.filters)]

    def execute(self):
        hit = self._match()
        if self.op == "select":
            hi = min(self.hi if self.hi is not None else 10**9, self.lo + CAP - 1)
            return SimpleNamespace(data=hit[self.lo : hi + 1], count=len(hit))
        if self.op == "delete":
            gone = hit[:CAP]
            ids = {id(r) for r in gone}
            self.db.tables[self.table] = [
                r for r in self.db.tables.get(self.table, []) if id(r) not in ids
            ]
            self.db.events.append(("delete", self.table, len(gone)))
            return SimpleNamespace(data=[], count=len(gone))
        for r in hit:
            r.update(self.patch)
        self.db.events.append(("update", self.table, len(hit)))
        return SimpleNamespace(data=hit, count=len(hit))


class FakeBucket:
    def __init__(self, db):
        self.db = db

    def list(self, path=None, options=None):
        options = options or {}
        prefix = f"{path}/"
        names, folders = set(), set()
        for p in self.db.files:
            if p.startswith(prefix):
                rest = p[len(prefix) :]
                if "/" in rest:
                    folders.add(rest.split("/", 1)[0])
                else:
                    names.add(rest)
        items = [{"name": n, "id": None} for n in sorted(folders)]
        items += [{"name": n, "id": f"obj-{n}"} for n in sorted(names)]
        off, lim = options.get("offset", 0), options.get("limit", 100)
        return items[off : off + lim]

    def remove(self, paths):
        assert len(paths) <= 100
        for p in paths:
            self.db.files.discard(p)
        self.db.events.append(("storage.remove", len(paths)))
        return []


class FakeAdmin:
    def __init__(self, db):
        self.db = db

    def list_users(self, page=1, per_page=50):
        users = [SimpleNamespace(id=u, email=e) for u, e in self.db.users.items()]
        return users[(page - 1) * per_page : page * per_page]

    def get_user_by_id(self, uid):
        if self.db.auth_error:
            raise self.db.auth_error
        if uid not in self.db.users:
            raise AuthApiError("User not found", 404, "user_not_found")
        return SimpleNamespace(user=SimpleNamespace(id=uid, email=self.db.users[uid]))

    def update_user_by_id(self, uid, attrs):
        self.db.events.append(("auth.ban", uid, attrs.get("ban_duration")))
        self.db.banned.add(uid)

    def delete_user(self, uid, should_soft_delete=False):
        self.db.events.append(("auth.delete_user", uid))
        self.db.users.pop(uid)


class FakeDB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.files: set[str] = set()
        self.users = {ME: EMAIL, OTHER: "other@example.com"}
        self.banned: set[str] = set()
        self.auth_error: Exception | None = None
        self.events: list[tuple] = []
        self.storage = SimpleNamespace(from_=lambda _b: FakeBucket(self))
        self.auth = SimpleNamespace(admin=FakeAdmin(self))

    def table(self, name):
        return FakeQuery(self, name)

    def writes(self):
        return [e for e in self.events if e[0] != "noop"]


def schema(**extra):
    """Live-shaped definitions: every rule's table with its columns."""
    defs: dict[str, dict] = {}
    for r in da.RULES:
        defs.setdefault(r.table, {"properties": {"id": {}}, "required": []})
        defs[r.table]["properties"][r.column] = {}
    defs["commissions"]["properties"]["referral_id"] = {}
    defs["referrals"]["required"] = ["id", "affiliate_id", "referred_user_id"]
    defs.update(extra)
    return defs


def no_stripe(_cus, _sub, _email):
    return [], []


def run(db, *argv, defs=None, stripe_check=no_stripe):
    return da.main(
        list(argv), sb=db, schema=defs if defs is not None else schema(), stripe_check=stripe_check
    )


@pytest.fixture
def db():
    d = FakeDB()
    d.tables["profiles"] = [
        {"id": "p1", "user_id": ME, "stripe_customer_id": None, "stripe_subscription_id": None},
        {"id": "p2", "user_id": OTHER},
    ]
    d.tables["applications"] = [{"id": f"a{i}", "user_id": ME} for i in range(3)] + [
        {"id": "ax", "user_id": OTHER}
    ]
    d.tables["extension_keys"] = [{"id": "k1", "user_id": ME, "email": EMAIL}]
    d.files = {f"{ME}/resume.pdf", f"{ME}/job_1_tailored.pdf", f"{OTHER}/resume.pdf"}
    return d


def _snapshot(db):
    return (
        {t: [dict(r) for r in rows] for t, rows in db.tables.items()},
        set(db.files),
        dict(db.users),
    )


def test_dry_run_deletes_nothing(db, capsys):
    before = _snapshot(db)
    assert run(db, "--email", EMAIL) == 0
    assert _snapshot(db) == before
    assert not [e for e in db.events if e[0] in ("delete", "update", "storage.remove")]
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "applications.user_id" in out and "2 file(s)" in out


def test_execute_erases_storage_and_rows_before_auth_user(db, capsys):
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME) == 0
    kinds = [e[0] for e in db.events]
    assert kinds[-1] == "auth.delete_user"
    first_delete = kinds.index("delete")
    assert kinds.index("storage.remove") < first_delete < kinds.index("auth.delete_user")
    assert db.files == {f"{OTHER}/resume.pdf"}
    assert [r["user_id"] for r in db.tables["applications"]] == [OTHER]
    assert [r["user_id"] for r in db.tables["profiles"]] == [OTHER]
    assert db.tables["extension_keys"] == []
    assert ME not in db.users and OTHER in db.users
    out = capsys.readouterr().out
    assert "receipt" in out and "2 file(s) removed" in out
    # extension_keys matched by user_id AND email — counted once on the receipt.
    assert out.count("extension_keys.") == 1


def test_user_id_flag_resolves_the_same_account(db):
    assert run(db, "--user-id", ME, "--execute", "--confirm", ME) == 0
    assert ME not in db.users


def test_confirm_mismatch_refuses(db):
    before = _snapshot(db)
    assert run(db, "--email", EMAIL, "--execute", "--confirm", OTHER) == 1
    assert run(db, "--email", EMAIL, "--execute", "--confirm", EMAIL) == 1  # email ≠ id
    assert run(db, "--email", EMAIL, "--execute") == 1
    assert _snapshot(db) == before


@pytest.mark.parametrize(
    "check",
    [
        lambda c, s, e: (["sub_1 (active)"], ["cus_1"]),
        lambda c, s, e: (["sub_1 (trialing)"], ["cus_1"]),
        lambda c, s, e: None,
    ],
    ids=["active", "trialing", "stripe-unreachable"],
)
def test_live_subscription_refuses(db, check, capsys):
    db.tables["profiles"][0].update(stripe_customer_id="cus_1", stripe_subscription_id="sub_1")
    before = _snapshot(db)
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME, stripe_check=check) == 2
    assert _snapshot(db) == before
    assert "REFUSED" in capsys.readouterr().err


def test_cancelled_subscription_proceeds(db):
    db.tables["profiles"][0].update(stripe_customer_id="cus_1", stripe_subscription_id=None)
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME) == 0


@pytest.mark.parametrize("money_table", ["commissions", "payouts"])
def test_affiliate_with_money_refuses(db, money_table, capsys):
    db.tables["affiliates"] = [{"id": "aff1", "user_id": ME, "code": "me"}]
    db.tables[money_table] = [{"id": "m1", "affiliate_id": "aff1", "referral_id": "r9"}]
    before = _snapshot(db)
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME) == 2
    assert _snapshot(db) == before
    assert "legal retention" in capsys.readouterr().err


def test_affiliate_without_money_is_erased(db):
    db.tables["affiliates"] = [{"id": "aff1", "user_id": ME, "code": "me"}]
    db.tables["referrals"] = [{"id": "r1", "affiliate_id": "aff1", "referred_user_id": OTHER}]
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME) == 0
    assert db.tables["affiliates"] == [] and db.tables["referrals"] == []


def test_referred_with_commissions_refuses_until_column_nullable(db, capsys):
    db.tables["referrals"] = [{"id": "r1", "affiliate_id": "affX", "referred_user_id": ME}]
    db.tables["commissions"] = [{"id": "c1", "affiliate_id": "affX", "referral_id": "r1"}]
    before = _snapshot(db)
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME) == 2
    assert _snapshot(db) == before
    assert "referrals_survive_account_erasure.sql" in capsys.readouterr().err


def test_referred_with_commissions_is_anonymized_when_nullable(db):
    db.tables["referrals"] = [{"id": "r1", "affiliate_id": "affX", "referred_user_id": ME}]
    db.tables["commissions"] = [{"id": "c1", "affiliate_id": "affX", "referral_id": "r1"}]
    defs = schema()
    defs["referrals"]["required"] = ["id", "affiliate_id"]
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME, defs=defs) == 0
    assert db.tables["referrals"] == [
        {"id": "r1", "affiliate_id": "affX", "referred_user_id": None}
    ]
    assert len(db.tables["commissions"]) == 1  # the affiliate's money is untouched


def test_referred_without_commissions_is_erased(db):
    db.tables["referrals"] = [{"id": "r1", "affiliate_id": "affX", "referred_user_id": ME}]
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME) == 0
    assert db.tables["referrals"] == []


def test_unclassified_user_column_fails_before_anything(db, capsys):
    defs = schema(new_feature={"properties": {"id": {}, "owner_user_id": {}}})
    before = _snapshot(db)
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME, defs=defs) == 3
    assert run(db, "--email", EMAIL, defs=defs) == 3
    assert _snapshot(db) == before
    assert "new_feature.owner_user_id" in capsys.readouterr().err


def test_every_live_person_column_is_classified():
    """The columns seen in prod on 2026-10-05 all have a rule."""
    live = {
        "activity_log": ["user_id"],
        "affiliate_applications": ["email", "paypal_email", "affiliate_id"],
        "affiliates": ["user_id", "paypal_email"],
        "applications": ["user_id"],
        "campaign_screenshots": ["user_id"],
        "campaign_states": ["user_id"],
        "commissions": ["affiliate_id"],
        "conversations": ["user_id"],
        "cover_letter_usage": ["user_id"],
        "extension_keys": ["user_id", "email"],
        "handbacks": ["user_id"],
        "interview_kits": ["user_id"],
        "jobs": ["user_id"],
        "outbound_log": ["user_id"],
        "payouts": ["affiliate_id"],
        "profiles": ["user_id"],
        "promo_redemptions": ["user_id"],
        "referrals": ["affiliate_id", "referred_user_id"],
        "screener_answer_cache": ["user_id"],
        "tap_reviews": ["user_id"],
        "training_examples": ["user_message"],
        "user_timezones": ["user_id"],
        "waitlist": ["email"],
    }
    defs = {t: {"properties": {c: {} for c in cols}} for t, cols in live.items()}
    assert da.unclassified_columns(defs) == []


def test_pagination_over_many_files_and_rows(db):
    db.files |= {f"{ME}/job_{1000 + i}_tailored.pdf" for i in range(250)}
    db.files |= {f"{ME}/sub/x{i}.pdf" for i in range(5)}  # a subfolder is walked too
    db.tables["activity_log"] = [{"id": f"l{i}", "user_id": ME} for i in range(2500)] + [
        {"id": "lx", "user_id": OTHER}
    ]
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME) == 0
    assert db.files == {f"{OTHER}/resume.pdf"}
    assert db.tables["activity_log"] == [{"id": "lx", "user_id": OTHER}]
    removes = [e for e in db.events if e[0] == "storage.remove"]
    assert sum(n for _, n in removes) == 257 and len(removes) == 3
    assert len([e for e in db.events if e[:2] == ("delete", "activity_log")]) == 3


def test_dry_run_lists_files_past_the_first_page(db, capsys):
    db.files |= {f"{ME}/job_{1000 + i}_tailored.pdf" for i in range(150)}
    assert run(db, "--email", EMAIL) == 0
    out = capsys.readouterr().out
    assert "152 file(s)" in out and "job_1149_tailored.pdf" in out


def test_storage_never_lists_outside_the_user_folder(db):
    with pytest.raises(ValueError):
        da.list_files(db, "")


def test_unknown_email_exits_1(db):
    assert run(db, "--email", "nobody@example.com") == 1


# --------------------------------------------------------------------------- review 10-06


@pytest.mark.parametrize(
    "defs",
    [
        {},  # OpenAPI off / v3 shape: `definitions` missing → {}
        "no-referrals",
        "no-required-list",
        "no-referral-id",
    ],
)
def test_partial_schema_fails_closed_and_keeps_others_money(db, defs, capsys):
    """The cascade the guard exists for: a referred user with commissions on them."""
    if defs == "no-referrals":
        defs = schema()
        del defs["referrals"]
    elif defs == "no-required-list":
        defs = schema()
        del defs["referrals"]["required"]
    elif defs == "no-referral-id":
        defs = schema()
        del defs["commissions"]["properties"]["referral_id"]
    db.tables["referrals"] = [{"id": "r1", "affiliate_id": "affX", "referred_user_id": ME}]
    db.tables["commissions"] = [{"id": "c1", "affiliate_id": "affX", "referral_id": "r1"}]
    before = _snapshot(db)
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME, defs=defs) == 3
    assert _snapshot(db) == before and not db.banned
    assert "SCHEMA GUARD" in capsys.readouterr().err


def test_schema_may_omit_extension_status(db):
    defs = schema()
    del defs["extension_status"]
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME, defs=defs) == 0


def test_stripe_is_asked_by_email_when_profile_has_no_customer(db, capsys):
    seen = []

    def by_email(cus, sub, email):
        seen.append((cus, sub, email))
        return (["sub_9 (active)"], ["cus_9"]) if email == EMAIL else ([], [])

    before = _snapshot(db)
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME, stripe_check=by_email) == 2
    assert seen == [(None, None, EMAIL)]
    assert _snapshot(db) == before
    assert "sub_9 (active)" in capsys.readouterr().err


def test_nothing_to_look_up_in_stripe_refuses(db, capsys):
    """--user-id for an account whose auth user is gone: no email, no stripe ids."""
    del db.users[ME]
    assert run(db, "--user-id", ME, "--execute", "--confirm", ME) == 2
    assert "No Stripe customer" in capsys.readouterr().err
    assert len(db.tables["applications"]) == 4


def test_stripe_check_finds_customer_by_email_and_counts_incomplete(monkeypatch):
    class Page(list):
        def auto_paging_iter(self):
            return iter(self)

    calls = []
    fake = SimpleNamespace(
        api_key=None,
        Customer=SimpleNamespace(
            list=lambda email, limit: calls.append(("cus", email)) or Page([{"id": "cus_e"}])
        ),
        Subscription=SimpleNamespace(
            list=lambda customer, status, limit: (
                Page(
                    [{"id": "sub_i", "status": "incomplete"}, {"id": "sub_c", "status": "canceled"}]
                )
                if customer == "cus_e"
                else Page([])
            ),
            retrieve=lambda _id: {"id": _id, "status": "canceled", "customer": "cus_e"},
        ),
    )
    monkeypatch.setitem(sys.modules, "stripe", fake)
    monkeypatch.setattr(da.config, "STRIPE_SECRET_KEY", "sk_test_x", raising=False)
    live, customers = da.stripe_live_subscriptions(None, None, EMAIL)
    assert calls == [("cus", EMAIL)]
    assert live == ["sub_i (incomplete)"] and customers == ["cus_e"]


def test_user_id_auth_error_is_not_absence(db, capsys):
    db.auth_error = AuthApiError("upstream timeout", 500, None)
    before = _snapshot(db)
    assert run(db, "--user-id", ME, "--execute", "--confirm", ME) == 1
    assert run(db, "--user-id", ME) == 1
    assert _snapshot(db) == before
    assert "Could not read auth user" in capsys.readouterr().err


def test_user_id_404_erases_rows_only(db, capsys):
    del db.users[ME]

    def ok(_c, _s, _e):
        return [], []

    db.tables["profiles"][0]["stripe_customer_id"] = "cus_1"  # something to ask Stripe by
    assert run(db, "--user-id", ME, "--execute", "--confirm", ME, stripe_check=ok) == 0
    assert [r["user_id"] for r in db.tables["applications"]] == [OTHER]
    assert "already absent" in capsys.readouterr().out


def test_ban_comes_before_any_erase(db):
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME) == 0
    kinds = [e[0] for e in db.events]
    assert kinds[0] == "auth.ban" and db.events[0][1] == ME
    assert kinds.index("auth.ban") < kinds.index("storage.remove") < kinds.index("delete")
    assert OTHER not in db.banned


def test_dry_run_and_refusal_do_not_ban(db):
    assert run(db, "--email", EMAIL) == 0
    db.tables["profiles"][0].update(stripe_customer_id="cus_1")
    live = lambda c, s, e: (["sub_1 (active)"], ["cus_1"])  # noqa: E731
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME, stripe_check=live) == 2
    assert not db.banned


def test_late_writes_are_swept_after_auth_delete(db, capsys):
    """A token issued before the ban lives out its hour: a row and a file land mid-erase."""
    real_delete_user = db.auth.admin.delete_user

    def delete_user_after_late_write(uid, **kw):
        db.tables.setdefault("activity_log", []).append({"id": "late", "user_id": ME})
        db.files.add(f"{ME}/late.pdf")
        real_delete_user(uid, **kw)

    db.auth.admin.delete_user = delete_user_after_late_write
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME) == 0
    assert db.tables["activity_log"] == []
    assert db.files == {f"{OTHER}/resume.pdf"}
    out = capsys.readouterr().out
    assert "late writes swept" in out and "activity_log.user_id 1" in out


def test_connect_account_named_on_receipt(db, capsys):
    db.tables["affiliates"] = [
        {"id": "aff1", "user_id": ME, "code": "me", "stripe_account_id": "acct_1"}
    ]
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME) == 0
    assert "Stripe Connect account acct_1" in capsys.readouterr().out


def test_live_schema_new_person_columns_are_classified():
    """Columns the wider USER_COLUMN pattern catches in prod (10-06) all have a home."""
    live = {
        "ad_spend": ["account_id"],
        "affiliates": ["user_id", "paypal_email", "stripe_account_id"],
        "profiles": ["user_id", "stripe_customer_id"],
    }
    defs = {t: {"properties": {c: {} for c in cols}} for t, cols in live.items()}
    assert da.unclassified_columns(defs) == []
    assert da.unclassified_columns({"x": {"properties": {"owner_id": {}}}}) == ["x.owner_id"]


def test_the_ai_call_ledger_is_erased_with_the_account(db):
    db.tables["ai_calls"] = [
        {"id": "c1", "user_id": ME, "cost_usd": "0.004"},
        {"id": "c2", "user_id": OTHER, "cost_usd": "0.002"},
    ]
    defs = schema(ai_calls_daily={"properties": {"day": {}, "user_id": {}}})
    assert da.unclassified_columns(defs) == []
    assert run(db, "--email", EMAIL, "--execute", "--confirm", ME) == 0
    assert db.tables["ai_calls"] == [{"id": "c2", "user_id": OTHER, "cost_usd": "0.002"}]
