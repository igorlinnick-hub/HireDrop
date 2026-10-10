"""Search-phrase yield — a phrase that keeps bringing nothing that fits becomes visible.

Why these tests exist: the batch judge decided whole Indeed pages ("0 fit you, 14 don't")
and threw the count away, so a phrase like "project manager" (construction, for a marketer)
was walked page after page, a minute of judging each, and the person never learned why
applications were few. What must hold:

  * a page counts only VERDICTS — company cap, applied, unjudged say nothing about a phrase;
  * an extension that sends no phrase is judged exactly as before and nothing is counted;
  * dry needs enough evidence (pages AND verdicts) and under 5% fits; unknown is never dry;
  * a new resume starts every phrase over, replacing one phrase does not wipe the others;
  * a failed write or read never costs the page — the read failing reads as "unknown".
"""

from unittest.mock import patch

from app.db import keyword_yield as ky
from tests.test_assess_fit_batch import _card, _row, _run


def test_a_page_counts_only_its_verdicts():
    with (
        patch.object(ky, "record_page") as record,
        patch.object(
            ky, "recent", return_value={"project manager": {"pages": 1, "judged": 2, "fits": 1}}
        ),
    ):
        out, *_ = _run(
            [_card(1), _card(2), _card(3, company="Taken")],
            [_row(1), _row(2), _row(3, company="Taken")],
            scores={"1": 60, "2": 12},
            history=["Taken"],
            keyword="  Project Manager ",
        )
    kwargs = record.call_args.kwargs
    assert (kwargs["judged"], kwargs["fits"]) == (2, 1)  # the capped card is not a verdict
    assert record.call_args.args[1] == "  Project Manager "
    assert out["keyword_yield"]["keyword"] == "project manager"


def test_an_old_extension_without_a_phrase_counts_nothing():
    with patch.object(ky, "record_page") as record, patch.object(ky, "recent") as read:
        out, *_ = _run([_card(1)], [_row(1)], scores={"1": 60})
    assert not record.called and not read.called
    assert "keyword_yield" not in out
    assert out["results"][0]["decision"] == "apply"


def test_dry_needs_pages_and_verdicts_and_under_five_percent_fits():
    assert ky.is_dry({"pages": 2, "judged": 20, "fits": 0})
    assert ky.is_dry({"pages": 3, "judged": 25, "fits": 1})  # 4%: the 10-10 project manager
    assert ky.is_dry({"pages": 5, "judged": 70, "fits": 3})  # 4.3%
    assert not ky.is_dry({"pages": 2, "judged": 20, "fits": 1})  # exactly 5% is not dry
    assert not ky.is_dry({"pages": 4, "judged": 60, "fits": 3})  # 5% where float 0.05*60 > 3
    assert not ky.is_dry({"pages": 5, "judged": 70, "fits": 4})  # 5.7% works
    assert not ky.is_dry({"pages": 1, "judged": 30, "fits": 0})  # one page is one bad page
    assert not ky.is_dry({"pages": 3, "judged": 12, "fits": 0})  # too few verdicts
    assert not ky.is_dry(None) and not ky.is_dry({})


def test_the_fingerprint_follows_the_resume_not_the_keywords():
    a = ky.yield_version({"keywords": ["pm"], "location": "San Diego"}, "resume A")
    b = ky.yield_version({"keywords": ["marketing pm"], "location": "San Diego"}, "resume A")
    c = ky.yield_version({"keywords": ["pm"], "location": "San Diego"}, "resume B")
    d = ky.yield_version({"keywords": ["pm"], "location": "Houston"}, "resume A")
    assert a == b  # replacing one phrase keeps what the others have shown
    assert a != c and a != d


def _rows(*specs):
    """(keyword, page_key, minute, judged, fits) -> rows as the table returns them."""
    return [
        {
            "id": i,
            "created_at": f"2026-10-09T{20 + minute // 60:02d}:{minute % 60:02d}:00+00:00",
            "keyword": kw,
            "page_key": key,
            "judged": judged,
            "fits": fits,
        }
        for i, (kw, key, minute, judged, fits) in enumerate(specs)
    ]


def _recent(rows):
    with patch.object(ky, "fetch_paged", return_value=rows), patch.object(ky, "get_supabase"):
        return ky.recent("u1", "v1")


def test_recent_sums_pages_per_phrase():
    got = _recent(
        _rows(
            ("project manager", None, 0, 14, 0),
            ("project manager", None, 5, 8, 0),
            ("social media", None, 6, 6, 3),
        )
    )
    assert got["project manager"] == {"pages": 2, "judged": 22, "fits": 0, "dry": True}
    assert got["social media"]["dry"] is False


def test_the_chunks_of_one_page_count_as_one_page():
    got = _recent(
        _rows(
            ("project manager", "0:0", 0, 5, 0),
            ("project manager", "0:0", 1, 5, 0),
            ("project manager", "0:0", 1, 4, 0),
        )
    )
    assert got["project manager"]["pages"] == 1 and got["project manager"]["judged"] == 14
    assert got["project manager"]["dry"] is False  # one page is one bad page


def test_the_same_page_key_in_a_later_run_is_a_new_page():
    got = _recent(
        _rows(
            ("project manager", "0:0", 0, 12, 0),
            ("project manager", "0:0", 1, 3, 0),
            ("project manager", "0:0", 150, 11, 0),  # next run restarts the key
        )
    )
    assert got["project manager"]["pages"] == 2
    assert got["project manager"]["dry"] is True


def test_a_batch_passes_the_page_key_through():
    with (
        patch.object(ky, "record_page") as record,
        patch.object(ky, "recent", return_value={}),
    ):
        _run([_card(1)], [_row(1)], scores={"1": 20}, keyword="pm", page="3:1")
    assert record.call_args.kwargs["page_key"] == "3:1"


def test_failures_never_cost_the_page():
    with patch.object(ky, "get_supabase", side_effect=RuntimeError("down")):
        ky.record_page("u1", "pm", "indeed", "v1", judged=5, fits=0)  # no raise
    with patch.object(ky, "fetch_paged", side_effect=RuntimeError("down")):
        assert ky.recent("u1", "v1") == {}  # unknown, never dry


def test_nothing_is_written_for_an_empty_phrase_or_a_page_without_verdicts():
    with patch.object(ky, "get_supabase") as sb:
        ky.record_page("u1", "   ", "indeed", "v1", judged=5, fits=0)
        ky.record_page("u1", "pm", "indeed", "v1", judged=0, fits=0)
    assert not sb.called


def test_the_dashboard_gets_every_profile_phrase_in_order():
    from app.routers import jobs

    class U:
        id = "u1"

    profile = {"keywords": ["Social Media", "project manager", "social media ", "Brand"]}
    tallies = {
        "project manager": {"pages": 2, "judged": 22, "fits": 0, "dry": True},
        "social media": {"pages": 3, "judged": 18, "fits": 4, "dry": False},
    }
    with (
        patch("app.db.profile.get_profile", return_value=profile),
        patch.object(jobs, "_deck_resume_text", return_value="resume"),
        patch.object(ky, "recent", return_value=tallies),
    ):
        got = jobs.get_keyword_yield(user=U())
    assert [k["keyword"] for k in got["keywords"]] == ["Social Media", "project manager", "Brand"]
    assert got["keywords"][2] == {
        "keyword": "Brand",
        "pages": 0,
        "judged": 0,
        "fits": 0,
        "dry": False,
    }
    assert got["all_dry"] is False and got["window_days"] == 7


def test_all_dry_is_said_out_loud():
    from app.routers import jobs

    class U:
        id = "u1"

    dry = {"pages": 2, "judged": 25, "fits": 0, "dry": True}
    with (
        patch("app.db.profile.get_profile", return_value={"keywords": ["a", "b"]}),
        patch.object(jobs, "_deck_resume_text", return_value="resume"),
        patch.object(ky, "recent", return_value={"a": dry, "b": dry}),
    ):
        assert jobs.get_keyword_yield(user=U())["all_dry"] is True
