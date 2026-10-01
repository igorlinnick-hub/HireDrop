"""Pure rules of the night-shift executor (scripts/night_shift) — no browser, no network."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "night_shift"))

import pytest  # noqa: E402
from ashby import application_url  # noqa: E402
from common import (  # noqa: E402
    _NO_OPT,
    _OPT_IN_Q,
    is_knockout,
    pick_typeahead,
    same_value,
    typeahead_kind,
    typeahead_queries,
    walk_verdict,
)


@pytest.mark.parametrize(
    "link",
    [
        "https://jobs.ashbyhq.com/acme/1234-abcd",
        "https://jobs.ashbyhq.com/acme/1234-abcd/application",
        "https://jobs.ashbyhq.com/acme/1234-abcd/application/application",
        "https://jobs.ashbyhq.com/acme/1234-abcd/application?utm_source=x",
    ],
)
def test_ashby_application_url_collapses_doubled_path(link):
    # The doubled path renders a form-less stub (pool rows, 08-04); the base is live.
    assert application_url(link) == "https://jobs.ashbyhq.com/acme/1234-abcd/application"


@pytest.mark.parametrize(
    "question,answer,expected",
    [
        ("Are you legally authorized to work in the United States?", "No", True),
        ("Are you currently located in the Bay Area?", "No", True),
        ("Are you legally authorized to work in the United States?", "Yes", False),
        # Sponsorship is the inverse question: "No" is the good answer.
        ("Will you now or in the future require visa sponsorship?", "No", False),
        ("How did you hear about us?", "No", False),
    ],
)
def test_knockout_rule(question, answer, expected):
    assert is_knockout(question, answer) is expected


# ── Typeahead fields: a profile FACT typed into a search box ─────────────────────────
# Labels and option shapes below are copied from the live DoorDash Greenhouse form that
# stopped the first server-side submit on 2026-09-30.


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("Location (City)", "location"),
        ("School", "school"),
        ("Degree", None),
        ("Discipline", None),
        ("Are you legally authorized to work in the United States?", None),
    ],
)
def test_typeahead_kind(label, kind):
    assert typeahead_kind(label) == kind


def test_school_is_typed_from_the_profile_then_other():
    assert typeahead_queries("school", {"school": "University of Hawaii at Manoa"}) == [
        "University of Hawaii at Manoa",
        "Other",
    ]


def test_no_fact_means_no_query():
    """Nothing on file = a hand-back. The model is never asked to pick a university."""
    assert typeahead_queries("school", {"school": ""}) == []
    assert typeahead_queries("school", {"school": "MIT", "no_degree": True}) == []
    assert typeahead_queries("location", {"city": "", "location": "remote"}) == []
    assert typeahead_queries("location", {"city": "", "location": "Honolulu, HI"}) == ["Honolulu"]


def test_school_pick_is_exact_or_unambiguous():
    opts = [
        "University of Hawaii at Hilo",
        "University of Hawaii at Manoa",
        "Hawaii Pacific University",
    ]
    assert pick_typeahead("school", "University of Hawaii at Manoa", opts, {}) == opts[1]
    assert pick_typeahead("school", "university of hawaii at manoa", opts, {}) == opts[1]
    # Two campuses contain the name — which one is not ours to choose.
    assert pick_typeahead("school", "University of Hawaii", opts, {}) is None
    assert pick_typeahead("school", "Other", ["Other", "Another School"], {}) == "Other"
    assert pick_typeahead("school", "Kyiv Polytechnic", opts, {}) is None


def test_location_pick_needs_the_state_when_the_city_is_ambiguous():
    opts = [
        "Portland, Oregon, United States",
        "Portland, Maine, United States",
        "Portland, Victoria, Australia",
    ]
    assert pick_typeahead("location", "Portland", opts, {"state": "ME"}) == opts[1]
    assert pick_typeahead("location", "Portland", opts, {"state": "Oregon"}) == opts[0]
    assert pick_typeahead("location", "Portland", opts, {"state": ""}) is None
    one = ["Honolulu, Hawaii, United States", "Honolulu County, Hawaii, United States"]
    assert pick_typeahead("location", "Honolulu", one, {"state": "HI"}) == one[0]
    assert pick_typeahead("location", "Honolulu", one, {"state": ""}) == one[0]
    assert pick_typeahead("location", "Honolulu", ["Hilo, Hawaii, United States"], {}) is None


# ── Reading a dropdown back, opt-ins, and when a walk stops ──────────────────────────


@pytest.mark.parametrize(
    ("wanted", "held", "same"),
    [
        ("Bachelor's Degree", "Bachelor's Degree", True),
        # The phone-country control shows less than its option said.
        ("United States +1", "+1", True),
        ("Yes", "Yes, I am authorized", True),
        ("Onsite", "Hybrid", False),
        # Nothing readable is NOT a match — the caller decides what an empty read means.
        ("Yes", "", False),
    ],
)
def test_same_value(wanted, held, same):
    assert same_value(wanted, held) is same


def test_sms_opt_in_is_declined_not_answered():
    """Label copied from the live DoorDash form (09-30), where the model answered Yes."""
    label = (
        "Would you like to receive communications via SMS and/or WhatsApp to the number"
        " provided above?"
    )
    assert _OPT_IN_Q.search(label)
    assert not _OPT_IN_Q.search("Are you legally authorized to work in the United States?")
    assert not _OPT_IN_Q.search("Have you worked at DoorDash?")
    options = ["Yes", "No"]
    assert next(o for o in options if _NO_OPT.match(o)) == "No"
    assert not _NO_OPT.match("None of the above")


BOARDS = ("greenhouse", "ashby")


def test_walk_stops_when_it_has_sent_what_it_was_allowed():
    assert walk_verdict(["sent"], 1, set(), BOARDS)
    assert walk_verdict(["handback", "sent", "knockout"], 2, set(), BOARDS) == ""
    assert walk_verdict(["handback", "sent", "knockout", "sent"], 2, set(), BOARDS)
    # A dry-run counts the forms it could have sent the same way.
    assert walk_verdict(["dry", "handback", "dry"], 2, set(), BOARDS)


def test_walk_stops_on_a_streak_of_refusals_only():
    assert walk_verdict(["captcha_code", "captcha_code"], 10, set(), BOARDS) == ""
    assert walk_verdict(["captcha_code", "invalid", "unknown"], 10, set(), BOARDS)
    assert walk_verdict(["error", "error", "error"], 10, set(), BOARDS)
    # A send in between resets it; hand-backs and knockouts are not the board refusing us.
    assert (
        walk_verdict(["captcha_code", "captcha_code", "sent", "captcha_code"], 10, set(), BOARDS)
        == ""
    )
    assert walk_verdict(["handback", "knockout", "handback", "knockout"], 10, set(), BOARDS) == ""


def test_walk_stops_only_when_every_board_is_capped():
    assert walk_verdict(["capped"], 10, {"greenhouse"}, BOARDS) == ""
    assert walk_verdict(["capped", "capped"], 10, {"greenhouse", "ashby"}, BOARDS)
    assert walk_verdict(["capped"], 10, {"greenhouse"}, ("greenhouse",))


# ── Answers that come from the profile, never from a model ───────────────────────────
# Every label below is copied from a live form walked on 2026-09-30.

from unittest.mock import patch  # noqa: E402

import common  # noqa: E402
from common import _SALARY_Q, parse_amount, salary_answer  # noqa: E402
from common import answer as night_answer  # noqa: E402

JOB = {"title": "Influencer Marketing Coordinator", "company": "Later", "description": ""}


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("$100,000 per year", 100_000),
        ("100k", 100_000),
        ("85K-95K", 85_000),
        ("$35/hour", 35),
        ("1.2m", 1_200_000),
        ("negotiable", None),
        ("", None),
    ],
)
def test_parse_amount(text, amount):
    assert parse_amount(text) == amount


def test_a_salary_question_is_never_sent_to_the_model():
    """The model answered "$55,000–$65,000" here for a user whose floor is $100,000."""
    label = "What are your salary expectations for this role?"
    assert _SALARY_Q.search(label)
    with patch.object(common, "answer_screener_question") as model:
        assert night_answer(label, JOB, {"salary_min": 100_000}) == ""
        assert night_answer(label, JOB, {"salary_expectation": "$100,000 per year"}) == (
            "$100,000 per year"
        )
        # Named, then withdrawn: "I'd rather not say" wins over a stale figure.
        assert (
            night_answer(
                label, JOB, {"salary_expectation": "$100,000", "no_salary_expectation": True}
            )
            == ""
        )
        model.assert_not_called()


def test_salary_is_shaped_for_the_control():
    profile = {"salary_expectation": "$100k per year"}
    assert salary_answer(profile, numeric=True) == "100000"
    ranges = ["Under $60,000", "$60,000 - $80,000", "$80,000 - $110,000", "$110,000+"]
    assert salary_answer(profile, ranges) == "$80,000 - $110,000"
    assert salary_answer({"salary_expectation": "$150,000"}, ranges) == "$110,000+"
    # An annual figure against hourly brackets is not "the nearest one".
    assert salary_answer(profile, ["$20 - $25", "$25 - $30"]) == ""
    # A yes/no about pay is the user's call, not arithmetic.
    assert salary_answer(profile, ["Yes", "No"]) == ""
    assert salary_answer({"salary_expectation": "negotiable"}, ranges) == ""
    assert salary_answer({"salary_expectation": "negotiable"}) == "negotiable"


def test_everything_else_still_reaches_the_shared_answerer():
    with patch.object(common, "answer_screener_question", return_value="Because.") as model:
        assert night_answer("Why are you interested in working at Suno?", JOB, {}) == "Because."
        assert not _SALARY_Q.search("Why are you interested in working at Suno?")
        model.assert_called_once()


def test_a_number_field_gets_a_number():
    """Ashby's "How many years…" is type=number; the answerer writes a sentence."""
    prose = "About 5 years, spanning agency-side campaign management (2020–2022)."
    label = (
        "How many years of experience do you have managing creative production and/or"
        " brand marketing campaigns?"
    )
    with patch.object(common, "answer_screener_question", return_value=prose):
        assert night_answer(label, JOB, {}, numeric=True) == "5"
        assert night_answer(label, JOB, {}) == prose
    with patch.object(common, "answer_screener_question", return_value="Several."):
        assert night_answer(label, JOB, {}, numeric=True) == ""


def test_language_the_role_is_built_on_is_a_knockout():
    q = "Do you have professional fluency in both Spanish and English?"
    assert is_knockout(q, "No")
    assert not is_knockout(q, "Yes")
    assert is_knockout("Are you fluent in German?", "No")
