"""A slow optional board must cost its own results — never the whole sweep.

`CraigslistPlatform.scrape` fans 20 cities over 8 workers with
`as_completed(futures, timeout=20)`. The timeout belongs to the ITERATOR, so it raises at the
for-loop's next() call — outside the try that guards a single `future.result()`. Under
throttling (~10s per city, ceil(20/8)*10 ≈ 30s) that TimeoutError escaped `scrape()` and
500'd the whole `/jobs/find` request, discarding every OTHER platform's already-scraped jobs.
Hypothesis from the 09-19 swarm report, verified on the source 09-26.
"""

import concurrent.futures as cf

from modules.platforms import craigslist as cl


def _job(city: str, n: int = 1) -> dict:
    return {
        "title": f"Welder {n}",
        "company": city.capitalize(),
        "link": f"https://{city}.craigslist.org/jjj/{n}.html",
        "date": "",
        "platform": "craigslist",
        "location": city,
        "job_type": "",
        "tags": [],
        "description": "",
    }


def test_the_iterator_timeout_does_not_escape_scrape(monkeypatch):
    monkeypatch.setattr(cl, "_scrape_city", lambda city, q, remote: [_job(city)])

    def times_out(futures, timeout=None):
        raise cf.TimeoutError()

    monkeypatch.setattr(cl.concurrent.futures, "as_completed", times_out)
    # The assertion is that this RETURNS — before the fix it raised into /jobs/find.
    assert cl.CraigslistPlatform().scrape(keywords=["welder"]) == []


def test_cities_that_answered_before_the_timeout_are_kept(monkeypatch):
    monkeypatch.setattr(cl, "_scrape_city", lambda city, q, remote: [_job(city)])
    real = cf.as_completed

    def one_then_timeout(futures, timeout=None):
        first = next(iter(real(futures)))
        yield first
        raise cf.TimeoutError()

    monkeypatch.setattr(cl.concurrent.futures, "as_completed", one_then_timeout)
    out = cl.CraigslistPlatform().scrape(keywords=["welder"])
    assert len(out) == 1
    assert out[0]["platform"] == "craigslist"


def test_a_healthy_sweep_is_unchanged(monkeypatch):
    monkeypatch.setattr(cl, "_scrape_city", lambda city, q, remote: [_job(city)])
    out = cl.CraigslistPlatform().scrape(keywords=["welder"], max_results=5)
    assert 1 <= len(out) <= 5
    assert {j["platform"] for j in out} == {"craigslist"}
