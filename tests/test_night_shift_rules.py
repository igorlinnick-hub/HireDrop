"""Pure rules of the night-shift executor (scripts/night_shift) — no browser, no network."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "night_shift"))

import pytest  # noqa: E402
from ashby import application_url  # noqa: E402
from common import is_knockout  # noqa: E402


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
