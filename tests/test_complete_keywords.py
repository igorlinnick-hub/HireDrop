"""complete_keywords — pad a thin keyword set from the résumé, bounded by apply mode.

The bug that made this necessary (measured 2026-09-22, account a0775013): a broad-mode
run carried four hand-typed words, one of them ("health care") pulling only licensed
clinical roles the résumé could never pass. The run opened 25 postings and applied to
ZERO — every gate worked, the INPUT was the ceiling. This raises the ceiling from the
résumé without asking the user to invent words, and does it WITHOUT inventing too many
(mode limit) or restating what they already typed (dedup).
"""

from modules.keyword_rotation import complete_keywords


def test_pads_thin_set_up_to_limit():
    full, added = complete_keywords(
        ["event manager", "social media"],
        ["marketing coordinator", "brand manager", "social media"],  # dup ignored
        limit=5,
    )
    assert full == ["event manager", "social media", "marketing coordinator", "brand manager"]
    assert added == ["marketing coordinator", "brand manager"]


def test_user_keywords_lead_and_are_never_dropped():
    full, added = complete_keywords(["promotion"], ["a", "b", "c", "d"], limit=3)
    assert full[0] == "promotion"  # user intent first
    assert added == ["a", "b"]  # only fills to the limit of 3


def test_already_full_changes_nothing():
    existing = ["a", "b", "c", "d", "e", "f", "g"]
    full, added = complete_keywords(existing, ["x", "y"], limit=7)
    assert full == existing
    assert added == []


def test_over_limit_is_never_trimmed():
    existing = ["a", "b", "c", "d"]
    full, added = complete_keywords(existing, ["x"], limit=3)  # user has more than broad? keep it
    assert full == existing
    assert added == []


def test_no_resume_roles_means_no_change():
    full, added = complete_keywords(["event manager"], [], limit=5)
    assert full == ["event manager"]
    assert added == []


def test_dedup_is_case_insensitive():
    full, added = complete_keywords(["Event Manager"], ["event manager", "PR manager"], limit=5)
    assert added == ["PR manager"]  # the case-variant dup is dropped


def test_zero_limit_is_a_noop():
    full, added = complete_keywords(["a"], ["b"], limit=0)
    assert full == ["a"]
    assert added == []
