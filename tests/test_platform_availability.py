"""A discovery source may never contribute a SILENT zero.

Both of our prod blind spots were shaped like this: the `jobspy`/`python-jobspy` package
mix-up (#113) and Google Jobs (Google now serves job results only to a real browser) each
returned [] forever while the dashboard still advertised the source as active. So the rule
is enforced here: a platform we can't fetch is skipped BEFORE the request, with a reason.
"""

from app.routers.jobs import SERVER_SCRAPE_SKIP, select_scrapeable
from modules.platforms.registry import PLATFORMS


def test_google_is_registered_but_marked_unavailable():
    google = PLATFORMS["google"]
    assert google.unavailable_reason, "google must carry a user-facing reason, not vanish"
    assert "browser" in google.unavailable_reason.lower()


def test_google_scrape_short_circuits_without_a_request():
    # No network, no JobSpy import: the class refuses to spend a request on a dead source.
    assert PLATFORMS["google"]().scrape(keywords=["marketing"], location="remote") == []


def test_unavailable_platform_is_skipped_with_a_note():
    scrapeable, notes = select_scrapeable(["google", "remoteok"])
    assert scrapeable == ["remoteok"]
    assert notes == [PLATFORMS["google"].unavailable_reason]


def test_indeed_still_skipped_server_side_with_its_own_note():
    # Indeed is discovered in-browser by the extension — never from our server.
    scrapeable, notes = select_scrapeable(["indeed"])
    assert scrapeable == []
    assert notes == [SERVER_SCRAPE_SKIP["indeed"]]


def test_unknown_platform_is_dropped_quietly():
    scrapeable, notes = select_scrapeable(["not_a_platform", "remoteok"])
    assert scrapeable == ["remoteok"]
    assert notes == []


def test_dead_scrapers_are_marked_rather_than_returning_a_silent_zero():
    # Measured live 2026-09-03 (scripts/audit_discovery_sources.py): Glassdoor answers
    # HTTP 400 "location not parsed" for every location, Wellfound's listing URL is a 404.
    for name in ("glassdoor", "wellfound"):
        assert PLATFORMS[name].unavailable_reason, f"{name} scraper is dead — say so"
        assert PLATFORMS[name]().scrape(keywords=["marketing"]) == []
        scrapeable, notes = select_scrapeable([name])
        assert scrapeable == [] and notes


def test_ziprecruiter_is_browser_side_discovery_not_a_server_scrape():
    # Its API 403s our server; the extension's native search-walk is what finds ZR jobs.
    scrapeable, notes = select_scrapeable(["ziprecruiter"])
    assert scrapeable == []
    assert notes == [SERVER_SCRAPE_SKIP["ziprecruiter"]]


def test_remoteok_keyword_phrase_matches_word_by_word():
    """The default platform used to return zero for any multi-word keyword.

    ?tags= hyphenated "marketing manager" into a tag RemoteOK does not have, so the feed came
    back empty and nobody saw an error. Now we filter locally: every word of a phrase must
    appear, in any order.
    """
    from modules.platforms.remoteok import RemoteOKPlatform

    feed = [
        {"legal": "header row"},
        {
            "position": "Senior Manager, Growth Marketing",
            "company": "Acme",
            "url": "https://remoteok.com/l/1",
            "description": "own the funnel",
            "tags": ["marketing"],
        },
        {
            "position": "Backend Engineer",
            "company": "Beta",
            "url": "https://remoteok.com/l/2",
            "description": "go and postgres",
            "tags": ["engineering"],
        },
    ]

    class FakeResponse:
        encoding = "utf-8"

        def raise_for_status(self):
            pass

        def json(self):
            return feed

    import modules.platforms.remoteok as mod

    original = mod.requests.get
    mod.requests.get = lambda *a, **kw: FakeResponse()
    try:
        jobs = RemoteOKPlatform().scrape(keywords=["marketing manager"])
        titles = [j["title"] for j in jobs]
    finally:
        mod.requests.get = original

    assert titles == ["Senior Manager, Growth Marketing"], titles


def test_every_registered_platform_either_scrapes_or_explains_itself():
    for name, cls in PLATFORMS.items():
        if cls.requires_credentials or name in SERVER_SCRAPE_SKIP:
            continue
        scrapeable, notes = select_scrapeable([name])
        assert scrapeable or notes, f"{name} would contribute a silent zero"
