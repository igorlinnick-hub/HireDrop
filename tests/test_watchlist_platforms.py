"""We only harvest boards we can actually apply to.

Workday fed 121 rows into the pool (9% of it) that no run could ever submit: the
extension walks greenhouse/lever/ashby only, and STATUS_MATRIX has workday DEFERRED
until there is an account for the multi-step apply. ats_boards.py even said so in a
comment — "scrapeable coverage but the extension never walks them" — and the harvest
ran anyway. Until #192 those rows also had empty descriptions, which made the scorer
rank them FIRST.

This test is the guard: adding a board for a platform the extension cannot walk has
to fail here rather than surface as unreachable inventory months later.
"""

from data.ats_watchlist import SEED_WATCHLIST, WORKDAY_BOARDS_PARKED

# Mirrors background.js: `const ATS_PLATFORMS = ["greenhouse", "lever", "ashby"]`.
# If the extension learns a new platform, widen this list in the same change.
APPLYABLE = {"greenhouse", "lever", "ashby"}


def test_every_harvested_board_is_one_we_can_apply_to():
    unreachable = sorted({p for _, p in SEED_WATCHLIST} - APPLYABLE)
    assert not unreachable, (
        f"harvesting boards the extension cannot walk: {unreachable}. "
        "Either teach the extension (background.js ATS_PLATFORMS) or park them."
    )


def test_workday_boards_are_parked_not_deleted():
    """Re-enabling should be pasting a list back, not re-researching nine tenants."""
    assert len(WORKDAY_BOARDS_PARKED) == 9
    assert all(platform == "workday" for _, platform in WORKDAY_BOARDS_PARKED)
    assert all("|" in token for token, _ in WORKDAY_BOARDS_PARKED), (
        "a Workday token encodes tenant|datacenter|site — losing that is losing the board"
    )


def test_parked_boards_are_not_also_live():
    live = {token for token, _ in SEED_WATCHLIST}
    assert not live & {token for token, _ in WORKDAY_BOARDS_PARKED}
