"""Employment type for a posting, read off the posting itself.

The `job_type` column existed and the dashboard has had a Full-time / Part-time / Contract
picker for months — but nothing ever WROTE the column for ATS boards. Measured on Igor's
pool 2026-09-08: 0 of 558 swipeable rows carried a job type, so "filter by type" could not
have worked no matter where the filter lived. Same shape as the address field that one user
in 28 ever filled: a control with no source behind it.

The boards state it plainly, usually in the title ("… (Contract)", "Part-Time Barista") and
otherwise in the first lines of the description. So this is deterministic string work, not
an AI call: the pool is filled 160 rows at a time and a model call per row would dominate
the cost of discovery.

Returns None when the posting doesn't say. None is a real answer and must stay distinct
from "full-time": a filter that treats silence as a match is guessing on the user's behalf.
"""

import re

# Order matters: the narrower types win. An internship is usually also called part-time,
# and a contract role often says "full-time contract" — the contract is the fact the user
# is filtering on.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("internship", re.compile(r"\b(intern|internship|co-?op)\b", re.I)),
    (
        "contract",
        re.compile(
            r"\b(contract|contractor|freelance|temporary|temp|c2c|1099|fixed[- ]term)\b", re.I
        ),
    ),
    ("part-time", re.compile(r"\bpart[\s-]?time\b", re.I)),
    ("full-time", re.compile(r"\bfull[\s-]?time\b", re.I)),
]

# How far into the description the employment type is still being STATED rather than
# mentioned in passing ("our contract with…", "full-time equivalent of…"). The boards put
# it in the header block; past that it is prose.
_DESCRIPTION_WINDOW = 400


def detect_job_type(title: str = "", description: str = "") -> str | None:
    """The posting's employment type, or None when it doesn't say.

    The title is checked on its own first: "Marketing Media Strategist, International
    (Contract)" is the board telling us outright, and it must not be outvoted by the word
    "full-time" appearing later in a benefits paragraph.
    """
    for text in ((title or ""), (description or "")[:_DESCRIPTION_WINDOW]):
        if not text.strip():
            continue
        for job_type, pattern in _PATTERNS:
            if pattern.search(text):
                return job_type
    return None


def matches_job_type(row_type: str | None, wanted: str | None) -> bool:
    """Does a pool row satisfy the user's type filter?

    Unknown type PASSES. The pool is full of rows harvested before this module existed, and
    hiding every one of them would empty the deck to prove a point — the honest failure
    here is showing a card the user then skips, not showing nothing at all. Rows do become
    filterable as they are re-harvested.
    """
    if not wanted:
        return True
    if not row_type:
        return True
    return row_type == wanted
