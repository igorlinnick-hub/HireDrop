"""Pure rules of the night-shift executor (scripts/night_shift) — no browser, no network."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "night_shift"))

from unittest.mock import patch  # noqa: E402

import common  # noqa: E402
import pytest  # noqa: E402
from ashby import _the_typed_one, application_url  # noqa: E402
from common import (  # noqa: E402
    _DECLINE_OPT,
    _DEMOGRAPHIC_Q,
    _NO_OPT,
    amounts,
    is_knockout,
    is_opt_in,
    number_from,
    parse_amount,
    pay_question,
    pick_typeahead,
    salary_answer,
    same_value,
    typeahead_kind,
    typeahead_queries,
    walk_verdict,
)
from common import answer as night_answer  # noqa: E402


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
# Labels and option shapes are copied from the live DoorDash Greenhouse form that stopped
# the first server-side submit on 2026-09-30; the wrong-pick cases are the ones an
# adversarial pass reproduced against the first version of these rules.


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("Location (City)", "location"),
        ("School", "school"),
        ("School or university", "school"),
        ("University name", "school"),
        ("Degree", None),
        ("Discipline", None),
        ("Are you legally authorized to work in the United States?", None),
        # A label that MENTIONS a school is not the school field: these are a fixed list
        # and a yes/no, and a university typed into them picked "Other".
        ("Highest level of school completed", None),
        ("Did you graduate from college?", None),
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
    assert typeahead_queries("location", {"city": "Honolulu", "location": "Houston, TX"}) == [
        "Honolulu"
    ]
    # `location` is where they SEARCH, not where they live — it is never typed as a home.
    assert typeahead_queries("location", {"city": "", "location": "San Francisco, CA"}) == []


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
    assert pick_typeahead("school", "Kyiv Polytechnic", opts, {}) is None
    longer = ["University of Hawaii at Manoa (UH)", "University of Hawaii at Hilo"]
    assert pick_typeahead("school", "University of Hawaii at Manoa", longer, {}) == longer[0]


@pytest.mark.parametrize(
    ("query", "options"),
    [
        ("MIT", ["Massachusetts Institute of Technology", "Smith College"]),
        ("USC", ["Tusculum University"]),
        ("Rice", ["Price College"]),
        ("Other", ["Mother Teresa Women's University"]),
    ],
)
def test_letters_inside_another_name_are_not_a_match(query, options):
    assert pick_typeahead("school", query, options, {}) is None


def test_other_is_taken_only_as_itself():
    assert pick_typeahead("school", "Other", ["Other", "Another School"], {}) == "Other"


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


@pytest.mark.parametrize(
    ("city", "state", "options"),
    [
        ("Portland", "ME", ["Portland, Oregon, United States", "Portland, Victoria, Australia"]),
        ("Paris", "TN", ["Paris, Texas, United States", "Paris, Ile-de-France, France"]),
        ("Athens", "OH", ["Athens, Georgia, United States", "Athens, Attica, Greece"]),
        ("Washington", "PA", ["Washington, District of Columbia, United States"]),
    ],
)
def test_a_known_state_is_never_overridden_by_the_only_us_hit(city, state, options):
    """The search did not offer their city; the lone American namesake is someone else's."""
    assert pick_typeahead("location", city, options, {"state": state}) is None


# ── Reading a dropdown back, opt-ins, and when a walk stops ──────────────────────────


@pytest.mark.parametrize(
    ("wanted", "held", "same"),
    [
        ("Bachelor's Degree", "Bachelor's Degree", True),
        ("Honolulu, Hawaii, United States", "honolulu,  hawaii, united states", True),
        # The phone-country control shows LESS than its option said.
        ("United States +1", "+1", True),
        # Loose containment would have passed every one of these wrong clicks.
        ("No", "None of the above", False),
        ("No", "Not sure", False),
        ("Yes", "Yes, with sponsorship", False),
        ("Male", "Female", False),
        ("Onsite", "Hybrid", False),
        ("2", "2-3 years", False),
        # Nothing readable is NOT a match — the caller decides what an empty read means.
        ("Yes", "", False),
    ],
)
def test_same_value(wanted, held, same):
    assert same_value(wanted, held) is same


def test_a_held_text_that_is_another_option_is_that_other_option():
    """Wanted the long answer, the widget holds the short one that is ALSO on the list."""
    options = ["Yes", "Yes, with sponsorship", "No"]
    assert same_value("Yes, with sponsorship", "Yes", options) is False
    assert same_value("Yes", "Yes", options) is True
    # …while a genuinely abbreviated display, which is nobody's option, still passes.
    assert same_value("United States +1", "+1", ["United States +1", "Canada +1"]) is True


@pytest.mark.parametrize(
    "label",
    [
        # Copied from the live DoorDash form (09-30), where the model answered Yes.
        "Would you like to receive communications via SMS and/or WhatsApp to the number"
        " provided above about your application process?",
        "I agree to receive text messages from Later",
        "Would you like to join our talent community?",
        "Subscribe to our newsletter",
        "Can we contact you by SMS?",
        "Opt-in to job alerts",
    ],
)
def test_an_opt_in_is_someone_asking_leave_to_contact_you(label):
    assert is_opt_in(label)
    assert next(o for o in ["Yes", "No"] if _NO_OPT.match(o)) == "No"
    assert not _NO_OPT.match("None of the above")


@pytest.mark.parametrize(
    "label",
    [
        # The word alone is not the shape: each of these got a "No" for a marketing
        # candidate without the model being asked.
        "Have you managed email and SMS programs in Klaviyo or Attentive?",
        "Describe your experience running SMS marketing campaigns",
        "How quickly are you adopting new tools?",
        "Are you comfortable receiving calls from customers?",
        "Have you written a newsletter before?",
        "What mechanisms do you use for opting out customers?",
        "Are you legally authorized to work in the United States?",
        "Have you worked at DoorDash?",
    ],
)
def test_a_question_that_mentions_a_channel_is_not_an_opt_in(label):
    assert not is_opt_in(label)


BOARDS = ("greenhouse", "ashby")


def test_walk_stops_when_it_has_sent_what_it_was_allowed():
    assert walk_verdict(["sent"], 1, set(), BOARDS)
    assert walk_verdict(["handback", "sent", "knockout"], 2, set(), BOARDS) == ""
    assert walk_verdict(["handback", "sent", "knockout", "sent"], 2, set(), BOARDS)
    # A dry-run counts the forms it could have sent the same way.
    assert walk_verdict(["dry", "handback", "dry"], 2, set(), BOARDS)


def test_walk_stops_on_three_refused_submits_however_they_are_spaced():
    assert walk_verdict(["captcha_code", "captcha_code"], 10, set(), BOARDS) == ""
    assert walk_verdict(["captcha_code", "invalid", "unknown"], 10, set(), BOARDS)
    assert walk_verdict(["error", "error", "error"], 10, set(), BOARDS)
    # Counted in a row, a hand-back between two refusals reset the count — a simulated
    # walk clicked Submit twenty times this way without one confirmed send.
    assert walk_verdict(["unknown", "handback"] * 3, 10, set(), BOARDS)
    assert walk_verdict(
        ["captcha_code", "sent", "captcha_code", "sent", "unknown"], 10, set(), BOARDS
    )
    # Hand-backs and knockouts are not the board refusing us, in any number.
    assert walk_verdict(["handback", "knockout"] * 5, 10, set(), BOARDS) == ""


def test_walk_stops_only_when_every_board_is_capped():
    assert walk_verdict(["capped"], 10, {"greenhouse"}, BOARDS) == ""
    assert walk_verdict(["capped", "capped"], 10, {"greenhouse", "ashby"}, BOARDS)
    assert walk_verdict(["capped"], 10, {"greenhouse"}, ("greenhouse",))


# ── Answers that come from the profile, never from a model ───────────────────────────
# Every label below is copied from a live form walked on 2026-09-30, or from the
# adversarial pass over the first version of these rules.

JOB = {"title": "Influencer Marketing Coordinator", "company": "Later", "description": ""}
BRACKETS = ["Under $60,000", "$60,000 - $80,000", "$80,000 - $110,000", "$110,000+"]


@pytest.mark.parametrize(
    ("text", "figures"),
    [
        ("$100,000 per year", [100_000]),
        ("100k", [100_000]),
        # A trailing k belongs to the whole range.
        ("85-95k", [85_000, 95_000]),
        ("$50k-$75k", [50_000, 75_000]),
        ("$100,000 - $120,000", [100_000, 120_000]),
        ("$35/hour", [35]),
        ("1.5k", [1_500]),
        ("1.2m", [1_200_000]),
        ("negotiable", []),
        ("", []),
    ],
)
def test_amounts(text, figures):
    assert amounts(text) == figures
    assert parse_amount(text) == (figures[0] if figures else None)


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("What are your salary expectations for this role?", "expectation"),
        ("What is your desired salary?", "expectation"),
        ("Desired salary", "expectation"),
        ("Salary", "expectation"),
        ("desired base pay", "expectation"),
        ("Expected OTE?", "expectation"),
        ("desired income", "expectation"),
        ("How much do you expect to be paid?", "expectation"),
        ("Desired hourly pay", "expectation"),
        ("Are you comfortable with the salary range of $55k-$65k for this role?", "expectation"),
        # What they earn NOW is another fact nobody told us — never the expectation.
        ("What is your current base pay?", "current"),
        ("Current salary", "current"),
        # The word alone is not the ask: "100000" was typed into the first of these.
        (
            "How many years of experience do you have in compensation and benefits administration?",
            None,
        ),
        ("Describe your experience designing compensation plans", None),
        ("Have you ever paid for our product?", None),
        ("Why are you interested in working at Suno?", None),
    ],
)
def test_pay_question(label, kind):
    assert pay_question(label) == kind


def test_a_salary_question_is_never_sent_to_the_model():
    """The model answered "$55,000–$65,000" here for a user whose floor is $100,000."""
    label = "What are your salary expectations for this role?"
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
        # Their CURRENT pay is not their expectation, and not the model's to guess.
        assert night_answer("Current salary", JOB, {"salary_expectation": "$100,000"}) == ""
        model.assert_not_called()


@pytest.mark.parametrize(
    ("stated", "options", "picked"),
    [
        ("$100,000 per year", BRACKETS, "$80,000 - $110,000"),
        ("$150,000", BRACKETS, "$110,000+"),
        ("$50,000", BRACKETS, "Under $60,000"),
        ("85-95k", BRACKETS, "$80,000 - $110,000"),
        # A shared bound belongs to the bracket that starts there.
        ("$100,000+", ["$75,000 - $100,000", "$100,000 - $125,000"], "$100,000 - $125,000"),
        ("$125,000", ["$75,000 - $100,000", "$100,000 - $125,000"], "$100,000 - $125,000"),
        ("$30/hr", ["Less than $25/hr", "$25-$35/hr", "More than $35/hr"], "$25-$35/hr"),
        # No bracket holds the figure → no answer. The first three ARE the incident: the
        # nearest bracket to $100,000 is the very number the model had made up.
        ("$100,000 per year", ["$55,000 - $65,000", "$60,000 - $75,000", "$70,000 - $85,000"], ""),
        ("$100,000 per year", ["$40,000 - $60,000", "$60,000 - $70,000"], ""),
        ("$100,000 per year", ["Prefer not to say", "$50k-$75k"], ""),
        # Another unit is not "the nearest one".
        ("$35/hour", BRACKETS, ""),
        ("$8,000/month", BRACKETS, ""),
        ("$30,000", ["Less than $25/hr", "$25-$35/hr", "More than $35/hr"], ""),
        ("$100,000 per year", ["$20 - $25", "$25 - $30"], ""),
        # A yes/no about pay is the user's call, not arithmetic.
        ("$100,000 per year", ["Yes", "No"], ""),
        ("negotiable", BRACKETS, ""),
    ],
)
def test_salary_bracket_holds_the_figure_or_nothing_is_picked(stated, options, picked):
    assert salary_answer({"salary_expectation": stated}, options) == picked


def test_salary_number_only_where_the_unit_is_the_users():
    yearly = {"salary_expectation": "$100k per year"}
    hourly = {"salary_expectation": "$35/hour"}
    assert salary_answer(yearly, numeric=True, label="Desired salary") == "100000"
    assert salary_answer({"salary_expectation": "85-95k"}, numeric=True, label="Salary") == "85000"
    # A bare number carries no unit: 35 in an annual-salary box is a different claim.
    assert salary_answer(hourly, numeric=True, label="Desired salary") == ""
    assert salary_answer(hourly, numeric=True, label="Desired hourly rate") == "35"
    assert salary_answer(yearly, numeric=True, label="Desired hourly rate") == ""
    # Free text gets the user's own words, whatever the unit.
    assert salary_answer(hourly) == "$35/hour"
    assert salary_answer({"salary_expectation": "negotiable"}) == "negotiable"


def test_a_pay_figure_the_model_made_up_is_dropped():
    """The net under pay_question: an unrecognised pay phrasing still reaches the model."""
    label = "Thoughts on pay for a role like this one, in your own words?"
    assert pay_question(label) is None
    with patch.object(common, "answer_screener_question", return_value="Around $65,000."):
        assert night_answer(label, JOB, {}) == ""
    with patch.object(common, "answer_screener_question", return_value="Open to discussing it."):
        assert night_answer(label, JOB, {}) == "Open to discussing it."


def test_everything_else_still_reaches_the_shared_answerer():
    with patch.object(common, "answer_screener_question", return_value="Because.") as model:
        assert night_answer("Why are you interested in working at Suno?", JOB, {}) == "Because."
        model.assert_called_once()


@pytest.mark.parametrize(
    ("reply", "label", "number"),
    [
        # Ashby's field is type=number; the answerer writes a sentence.
        (
            "About 5 years, spanning agency-side campaign management (2020–2022).",
            "How many years",
            "5",
        ),
        # The figure next to "years" wins; a calendar year is never an amount.
        (
            "Since 2019 I've run paid social for about 6 years.",
            "How many years of experience?",
            "6",
        ),
        ("2019", "How many years of experience?", ""),
        ("I managed a team of 12.", "How many people have you managed?", "12"),
        ("Several.", "How many years", ""),
        ("3", "Years of experience", "3"),
    ],
)
def test_number_from(reply, label, number):
    assert number_from(reply, label) == number
    with patch.object(common, "answer_screener_question", return_value=reply):
        assert night_answer(label, JOB, {}, numeric=True) == number


def test_language_the_role_is_built_on_is_a_knockout():
    q = "Do you have professional fluency in both Spanish and English?"
    assert is_knockout(q, "No")
    assert not is_knockout(q, "Yes")
    assert is_knockout("Are you fluent in German?", "No")


def test_website_is_the_candidates_own_site_or_nothing():
    """Tia's form (09-30): "Website" was answered with the LinkedIn URL a second time."""
    with patch.object(common, "answer_screener_question") as model:
        assert night_answer("Website", JOB, {"linkedin_url": "linkedin.com/in/x"}) == ""
        assert night_answer("Website", JOB, {"portfolio_url": " https://igor.example "}) == (
            "https://igor.example"
        )
        assert night_answer("Portfolio URL", JOB, {}) == ""
        model.assert_not_called()
    with patch.object(
        common, "answer_screener_question", return_value="linkedin.com/in/x"
    ) as model:
        # A LinkedIn field is not a website field, and a dropdown is not a URL box.
        assert night_answer("LinkedIn Profile / Website", JOB, {}) == "linkedin.com/in/x"
        night_answer("Which website did you find us on?", JOB, {}, ["LinkedIn", "Indeed"])
        assert model.call_count == 2


def test_hispanic_question_is_self_identification():
    """DoorDash and Later ask it without the word "ethnicity" (live forms, 09-30)."""
    for q in ("Are you Hispanic or Latinx?", "Are you Hispanic/Latino?", "Hispanic or Latina"):
        assert _DEMOGRAPHIC_Q.search(q), q
    assert not _DEMOGRAPHIC_Q.search("Do you speak Latin American Spanish?")
    assert _DECLINE_OPT.search("Decline To Self Identify")


def test_an_ashby_suggestion_is_taken_for_what_it_says_not_for_coming_first():
    assert _the_typed_one("Notion", ["Notion AI", "Notion"]) == "Notion"
    assert _the_typed_one("hubspot", ["HubSpot CRM"]) == "HubSpot CRM"
    # Two carry it — which one was meant is not ours to say.
    assert _the_typed_one("Google", ["Google Ads", "Google Analytics"]) is None
    # Letters inside another word are not the word.
    assert _the_typed_one("Other", ["Mother Teresa University"]) is None
    assert _the_typed_one("Rust", []) is None
