"""The job page heals a pool row the search card left without a company or a place.

Prod 09-22 → 10-06: 0 of 344 Indeed rows had a location, and an Indeed card for an
employer with no company page arrives with company "". On 10-06 the employer texted Igor
back ("Britney, HR at Adventure Loom") and the row read "Events Associate @ (blank)", no
city — nothing to find it by. The detail page has both fields; /jobs/describe already
carried the company, but save_description wrote the text and dropped the rest.

Blank-only: a name or place already stored is never rewritten.
"""

from unittest.mock import MagicMock, patch

from app.db.jobs import save_description
from modules.job_location import location_verdict, parse_user_location

LINK = "https://www.indeed.com/viewjob?jk=d8b40b219fcb72ca"
TEXT = "We are looking for an enthusiastic, organized Events Associate. " * 6


def _fake(existing):
    """A supabase stub: get_by_link's select returns `existing`; update/insert are recorded."""
    sb = MagicMock()
    table = sb.table.return_value
    select_chain = table.select.return_value.eq.return_value.eq.return_value.limit.return_value
    select_chain.execute.return_value = MagicMock(data=[existing] if existing else [])
    table.select.return_value.eq.return_value.ilike.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[]
    )
    table.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "j1"}]
    )
    table.insert.return_value.execute.return_value = MagicMock(data=[{"id": "j2"}])
    return sb, table


def test_blank_company_and_place_are_filled_from_the_page():
    sb, table = _fake({"id": "j1", "company": "", "location": None})
    with patch("app.db.jobs.get_supabase", return_value=sb):
        got = save_description(
            "u1",
            LINK,
            TEXT,
            company="Adventure Loom Htx",
            location="Houston, TX 77074",
            platform="indeed",
        )
    assert got == "j1"
    assert table.update.call_args.args[0] == {
        "description": TEXT,
        "company": "Adventure Loom Htx",
        "location": "Houston, TX 77074",
    }


def test_a_stored_name_or_place_is_never_rewritten():
    sb, table = _fake({"id": "j1", "company": "Adventure Loom", "location": "Houston, TX"})
    with patch("app.db.jobs.get_supabase", return_value=sb):
        save_description("u1", LINK, TEXT, company="Somebody Else LLC", location="Austin, TX")
    assert table.update.call_args.args[0] == {"description": TEXT}


def test_an_empty_page_read_writes_nothing_but_the_text():
    sb, table = _fake({"id": "j1", "company": "", "location": ""})
    with patch("app.db.jobs.get_supabase", return_value=sb):
        save_description("u1", LINK, TEXT, company="   ", location="")
    assert table.update.call_args.args[0] == {"description": TEXT}


def test_the_update_stays_scoped_to_the_user():
    sb, table = _fake({"id": "j1", "company": "", "location": ""})
    with patch("app.db.jobs.get_supabase", return_value=sb):
        save_description("u1", LINK, TEXT, location="Houston, TX 77074")
    eq = table.update.return_value.eq
    assert eq.call_args.args == ("id", "j1")
    assert eq.return_value.eq.call_args.args == ("user_id", "u1")


def test_a_by_link_insert_keeps_the_place():
    sb, table = _fake(None)
    with patch("app.db.jobs.get_supabase", return_value=sb):
        save_description(
            "u1",
            LINK,
            TEXT,
            title="Events Associate",
            company="Adventure Loom Htx",
            location="Houston, TX 77074",
            platform="indeed",
        )
    row = table.insert.call_args.args[0]
    assert row["location"] == "Houston, TX 77074"
    assert row["company"] == "Adventure Loom Htx"
    assert row["status"] == "new"


def test_the_place_the_page_gives_is_one_the_deck_filter_can_read():
    # The point of filling it: "" is "unknown" and passes every city filter.
    houston = parse_user_location("Houston, TX")
    assert location_verdict("", houston) == "unknown"
    assert location_verdict("Houston, TX 77074", houston) == "fits"
    assert location_verdict("Houston, TX 77074", parse_user_location("Miami, FL")) == "elsewhere"


def test_describe_endpoint_passes_the_place_through(auth_client):
    with patch("app.routers.jobs.jobs_db.save_description", return_value="j1") as save:
        res = auth_client.post(
            "/api/v1/jobs/describe",
            json={
                "link": LINK,
                "description": TEXT,
                "company": "Adventure Loom Htx",
                "location": "Houston, TX 77074",
                "platform": "indeed",
            },
        )
    assert res.status_code == 200
    assert save.call_args.kwargs["location"] == "Houston, TX 77074"
    assert save.call_args.kwargs["company"] == "Adventure Loom Htx"
