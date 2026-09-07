"""Unit tests for the ATS pool-driven consumption loop's job-selection logic
(GLOBAL_PLAN P1). These cover the correctness-determining pieces of the loop:
which jobs get queued (zero-touch filter), in what order (zero-touch first), and
the per-mode daily caps. Pure functions — no network, no DB.
"""

import modules.captcha_profile as cp
from app.db.subscriptions import (
    MAX_PER_PLATFORM,
    TAP_DAILY_LIMIT,
    TIER_LIMITS,
    daily_limit,
)
from app.routers.jobs import _with_captcha

# ---------- captcha_profile: the per-platform touch signal driving the loop ----------


def test_captcha_touch_per_platform():
    assert cp.captcha_touch("indeed") == "low"
    assert cp.captcha_touch("greenhouse") == "low"  # zero-touch → full-auto pool
    assert cp.captcha_touch("lever") == "high"  # hCaptcha → human/tapalka
    assert cp.captcha_touch("ziprecruiter") == "medium"
    assert cp.captcha_touch("unknown-platform") == "medium"  # safe default


def test_is_zero_touch():
    assert cp.is_zero_touch("greenhouse") is True
    assert cp.is_zero_touch("indeed") is True
    assert cp.is_zero_touch("lever") is False


def test_captcha_touch_case_insensitive():
    assert cp.captcha_touch("GREENHOUSE") == "low"


def test_touch_rank_orders_low_first():
    assert cp.TOUCH_RANK["low"] < cp.TOUCH_RANK["medium"] < cp.TOUCH_RANK["high"]


# ---------- ats_boards: host filtering + tagging + discovery ranking ----------


def test_is_fillable_only_greenhouse_lever_hosts():
    from modules.platforms.ats_boards import _is_fillable

    assert _is_fillable("https://job-boards.greenhouse.io/figma/jobs/1") is True
    assert _is_fillable("https://jobs.lever.co/acme/uuid/apply") is True
    # custom career-domain embeds are NOT phase_ats-fillable → dropped
    assert _is_fillable("https://stripe.com/jobs?gh_jid=123") is False
    assert _is_fillable("") is False


def test_job_is_tagged_zero_touch_by_platform():
    from modules.platforms.ats_boards import _job

    gh = _job(
        "Marketing", "figma", "https://job-boards.greenhouse.io/figma/jobs/1", "NY", "greenhouse"
    )
    lv = _job("Marketing", "acme", "https://jobs.lever.co/acme/x/apply", "SF", "lever")
    assert gh["captcha_touch"] == "low" and gh["zero_touch"] is True
    assert lv["captcha_touch"] == "high" and lv["zero_touch"] is False
    assert gh["link"] == gh["apply_url"]  # link = the direct apply URL the filler navigates to


def test_discover_ats_ranks_zero_touch_first(monkeypatch):
    """discover_ats must return low-touch (Greenhouse) before high-touch (Lever) so the
    pool/queue fills with zero-touch destinations first."""
    import modules.platforms.ats_boards as ab

    def fake_gh(token, keywords=None, limit=50):
        return [
            ab._job(
                f"GH {token}",
                token,
                f"https://job-boards.greenhouse.io/{token}/jobs/1",
                "",
                "greenhouse",
            )
        ]

    def fake_lv(token, keywords=None, limit=50):
        return [
            ab._job(f"LV {token}", token, f"https://jobs.lever.co/{token}/1/apply", "", "lever")
        ]

    monkeypatch.setitem(ab._FETCHERS, "greenhouse", fake_gh)
    monkeypatch.setitem(ab._FETCHERS, "lever", fake_lv)

    # Lever listed FIRST in the input — output must still be Greenhouse-first.
    out = ab.discover_ats([("acme", "lever"), ("figma", "greenhouse")], keywords=None, cap=10)
    touches = [j["captcha_touch"] for j in out]
    assert touches == sorted(touches, key=lambda t: ab._TOUCH_RANK[t])  # monotonic low→high
    assert out[0]["platform"] == "greenhouse"
    assert out[-1]["platform"] == "lever"


def test_discover_ats_dedups_by_apply_url(monkeypatch):
    import modules.platforms.ats_boards as ab

    dup = "https://job-boards.greenhouse.io/figma/jobs/1"

    def fake_gh(token, keywords=None, limit=50):
        return [
            ab._job("A", token, dup, "", "greenhouse"),
            ab._job("B", token, dup, "", "greenhouse"),
        ]

    monkeypatch.setitem(ab._FETCHERS, "greenhouse", fake_gh)
    out = ab.discover_ats([("figma", "greenhouse")], cap=10)
    assert len(out) == 1  # same apply_url deduped


# ---------- jobs enrichment: /jobs tags + orders zero-touch first (feeds the queue) ----------


def test_with_captcha_tags_and_sorts_zero_touch_first():
    jobs = [
        {"platform": "lever", "company": "acme", "date": 3},
        {"platform": "greenhouse", "company": "figma", "date": 1},
        {"platform": "ziprecruiter", "company": "zr", "date": 2},
        {"platform": "greenhouse", "company": "airtable", "date": 0},
    ]
    out = _with_captcha(jobs)
    # every job tagged
    assert all("captcha_touch" in j and "zero_touch" in j for j in out)
    # zero-touch (greenhouse) first, high-touch (lever) last
    assert out[0]["zero_touch"] is True
    assert out[-1]["platform"] == "lever"
    # stable within a touch band: the two greenhouse jobs keep their incoming order
    gh = [j for j in out if j["platform"] == "greenhouse"]
    assert [j["company"] for j in gh] == ["figma", "airtable"]


def test_with_captcha_empty_list():
    assert _with_captcha([]) == []


# ---------- caps: daily_limit by tier + submit_mode (bounds the queue + the loop) ----------


def test_daily_limit_auto_vs_tap_paid():
    assert daily_limit("pro", "auto") == TIER_LIMITS["pro"]  # 30
    assert (
        daily_limit("pro", "tap") == TAP_DAILY_LIMIT
    )  # 30 (=auto; tap is a quality lane, not a volume lift — 2026-08-02)
    assert daily_limit("premium", "tap") == TAP_DAILY_LIMIT


def test_daily_limit_free_stays_free_even_in_tap():
    assert daily_limit("free", "auto") == TIER_LIMITS["free"]  # 10
    assert daily_limit("free", "tap") == TIER_LIMITS["free"]  # tap does NOT lift free


def test_daily_limit_admin_unlimited():
    assert daily_limit("admin", "auto") > 1_000_000
    assert daily_limit("admin", "tap") > 1_000_000


def test_daily_limit_defaults_to_auto():
    assert daily_limit("pro") == TIER_LIMITS["pro"]  # no submit_mode → auto


def test_per_platform_rail_is_ban_safety_value():
    # 15/day per platform (Igor 2026-07-16, tap-pool era; was 20)
    assert MAX_PER_PLATFORM == 15


# ---------- watchlist ordering: relevance decides what survives the cap ----------


def test_vertical_boards_jump_the_queue_for_matching_keywords():
    # A sweep returns far fewer jobs than it collects, so a board's position decides
    # whether its inventory reaches the user at all. Non-tech boards sit at the end of the
    # curated list and never made the cut (measured 2026-09-06: 0 of 160 on Igor's own
    # keywords) until keyword-matched verticals started going first.
    from data.ats_watchlist import prioritized_boards

    boards = [("stripe", "greenhouse"), ("oscar", "greenhouse"), ("figma", "greenhouse")]
    assert prioritized_boards(boards, ["healthcare marketing"])[0] == ("oscar", "greenhouse")
    assert prioritized_boards(boards, ["restaurant server"]) == boards  # no hospitality board here


def test_untagged_search_keeps_the_curated_order():
    # The promotion must never reshuffle the list for everyone else: a tech search has to
    # behave exactly as it did before verticals existed.
    from data.ats_watchlist import prioritized_boards

    boards = [("stripe", "greenhouse"), ("oscar", "greenhouse"), ("figma", "greenhouse")]
    assert prioritized_boards(boards, ["senior software engineer"]) == boards
    assert prioritized_boards(boards, []) == boards
    assert prioritized_boards(boards, None) == boards


def test_every_tagged_board_is_actually_on_the_watchlist():
    # A tag on a board we don't sweep is dead config — it silently promotes nothing.
    from data.ats_watchlist import BOARD_VERTICALS, SEED_WATCHLIST, VERTICAL_HINTS

    tokens = {t.lower() for t, _ in SEED_WATCHLIST}
    assert not set(BOARD_VERTICALS) - tokens
    # …and every vertical a board claims must be one the keyword matcher can reach.
    claimed = {v for tags in BOARD_VERTICALS.values() for v in tags}
    assert not claimed - set(VERTICAL_HINTS)
