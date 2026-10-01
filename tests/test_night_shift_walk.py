"""The night-shift WALK, end to end, with every outside thing faked — no browser, no
network. These are the rules that decide how many real applications one unwatched run
sends, and whether it ever sends the same one twice.

Each scenario is one an adversarial pass reproduced against the first version of the
walk (2026-10-01): it sent two under `--max 1`, clicked Submit twenty times without one
confirmed send, and applied twice to a posting the pool held under two spellings.
"""

import asyncio
import os
import sys
import types
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "night_shift"))

# The executor imports Playwright at module level; CI does not install it (the browser is
# only needed to actually run the script). Nothing here touches a real browser, so a stub
# module is enough to import the code under test.
try:
    import playwright.async_api  # noqa: F401
except ImportError:  # pragma: no cover — CI
    stub = types.ModuleType("playwright.async_api")
    stub.async_playwright = lambda: None
    sys.modules.setdefault("playwright", types.ModuleType("playwright"))
    sys.modules["playwright.async_api"] = stub

import executor  # noqa: E402

USER = "user-1"
PROFILE = {
    "name": "Igor",
    "last_name": "L",
    "email": "x@example.com",
    "work_setting": "remote",
    "location": "Honolulu, HI",
    "state": "HI",
    "apply_mode": "broad",
}


class _Locator:
    def __init__(self, on_click):
        self._on_click = on_click

    async def count(self):
        return 1

    @property
    def first(self):
        return self

    async def click(self, *_a, **_k):
        self._on_click()


class _Form:
    def __init__(self, page, clicks):
        self._page, self._clicks = page, clicks

    def locator(self, _selector):
        return _Locator(lambda: self._clicks.append(self._page.url))


class _Page:
    url = "about:blank"

    async def goto(self, url, **_k):
        self.url = url

    async def wait_for_timeout(self, _ms):
        pass

    async def close(self):
        pass

    def on(self, *_a):
        pass

    async def screenshot(self, **_k):
        pass


class _Playwright:
    """`async with async_playwright() as pw: pw.chromium.launch() → context → page`."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    @property
    def chromium(self):
        return self

    async def launch(self, **_k):
        return self

    async def new_context(self, **_k):
        return self

    async def new_page(self):
        return _Page()

    async def close(self):
        pass


def _jobs(n, twin=False):
    rows = [
        {
            "id": f"j{i}",
            "title": f"Marketing Coordinator {i}",
            "company": f"Acme {i}",
            "platform": "greenhouse",
            "location": "Remote",
            "link": f"https://job-boards.greenhouse.io/acme/jobs/{8095311 + i}",
            "status": "new",
        }
        for i in range(n)
    ]
    if twin:  # the same posting under a second spelling, as the pool really holds them
        rows.insert(
            1,
            {
                **rows[0],
                "id": "j0-twin",
                "link": "https://boards.greenhouse.io/acme/jobs/8095311?gh_jid=8095311",
            },
        )
    return rows


class Walk:
    """One simulated `executor.run(--live)`; records what reached the outside world."""

    def __init__(self, jobs, *, judge=None, fill=None, save=None, history=None, knockouts=None):
        self.clicks: list[str] = []
        self.saved: list[str] = []
        self.handbacks: list[tuple[str, str]] = []  # (job id, outcome)
        self.retired: list[str] = []
        self._jobs, self._judge, self._fill, self._save = jobs, judge, fill, save
        self._history = history
        self._knockouts = knockouts

    async def _find(self, page):
        return _Form(page, self.clicks)

    async def _default_judge(self, _page, _form, _watch):
        return True, "confirmation url", "sent"

    async def _default_fill(self, *_a, **_k):
        return [], ""

    async def _no_knockouts(self, _form):
        return []

    def _default_save(self, **kw):
        self.saved.append(kw["job_url"])

    def _handback(self, _user, job, _platform, _unfilled, reason="", outcome="handback"):
        self.handbacks.append((job["id"], outcome))

    def run(self, max_jobs=1, live=True):
        history = self._history if self._history is not None else (lambda *_a, **_k: [])
        with (
            patch.object(executor, "get_profile", return_value=dict(PROFILE)),
            patch.object(executor, "pick_jobs", return_value=self._jobs),
            patch.object(executor, "download_resume", return_value="/tmp/r.pdf"),
            patch.object(executor, "verdict_version", return_value="v1"),
            patch.object(executor, "resume_text_for", return_value="resume"),
            patch.object(executor, "async_playwright", return_value=_Playwright()),
            patch.object(executor, "find_application_form", side_effect=self._find),
            patch.object(
                executor, "assess_fit", return_value={"decision": "apply", "fit_score": 80}
            ),
            patch.object(executor, "fill_greenhouse", side_effect=self._fill or self._default_fill),
            patch.object(
                executor, "knockout_answers", side_effect=self._knockouts or self._no_knockouts
            ),
            patch.object(
                executor, "check_can_apply", return_value={"allowed": True, "tier": "admin"}
            ),
            patch.object(
                executor, "submit_outcome", side_effect=self._judge or self._default_judge
            ),
            patch.object(executor, "record_handback", side_effect=self._handback),
            patch.object(executor, "_note"),
            patch.object(executor.os, "makedirs"),
            patch.object(executor.apps_db, "applied_job_urls", side_effect=history),
            patch.object(
                executor.apps_db, "save_application", side_effect=self._save or self._default_save
            ),
            patch.object(executor.jobs_db, "mark_applied_by_link", return_value=1),
            patch.object(
                executor.jobs_db,
                "mark_dead_link",
                side_effect=lambda _u, link: self.retired.append(link) or 1,
            ),
        ):
            return asyncio.run(executor.run(USER, None, live, False, ("greenhouse",), max_jobs))


def test_max_is_how_many_are_sent():
    walk = Walk(_jobs(10))
    assert walk.run(max_jobs=2) == ["sent", "sent"]
    assert len(walk.clicks) == 2 and len(walk.saved) == 2


def test_a_dry_run_never_clicks_submit():
    walk = Walk(_jobs(3))
    assert walk.run(max_jobs=2, live=False) == ["dry", "dry"]
    assert walk.clicks == [] and walk.saved == []


def test_a_crash_while_reading_the_result_is_not_a_second_application():
    """Past the click the application may be with the employer. The old walk called it an
    "error", sent the next one too under --max 1, and left the first to be re-sent."""
    calls = {"n": 0}

    async def judge(_page, _form, _watch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Timeout 30000ms exceeded")
        return True, "confirmation url", "sent"

    walk = Walk(_jobs(5), judge=judge)
    assert walk.run(max_jobs=1) == ["unknown", "sent"]
    # The first one is quarantined as a hand-back, so no later walk opens it again.
    assert walk.handbacks == [("j0", "unknown")]
    assert len(set(walk.clicks)) == len(walk.clicks) == 2


def test_three_refused_submits_end_the_walk_whatever_sits_between_them():
    """Counted "in a row", a hand-back between two refusals reset the count: forty forms,
    twenty Submit clicks, nothing confirmed, never stopped."""
    filled = {"n": 0}

    async def judge(_page, _form, _watch):
        return False, "no proof of sending (form still on screen, http 200)", "unknown"

    async def fill(*_a, **_k):
        filled["n"] += 1
        return (["School"], "") if filled["n"] % 2 == 0 else ([], "")

    walk = Walk(_jobs(60), judge=judge, fill=fill)
    outcomes = walk.run(max_jobs=1)
    assert outcomes.count("unknown") == 3 and len(walk.clicks) == 3
    assert outcomes[-1] == "unknown"
    # Every one of them is handed back — refused submits and the unanswered form alike.
    assert [o for _j, o in walk.handbacks] == [
        "unknown",
        "handback",
        "unknown",
        "handback",
        "unknown",
    ]


def test_one_posting_under_two_spellings_is_one_application():
    walk = Walk(_jobs(4, twin=True))
    assert walk.run(max_jobs=3) == ["sent", "sent", "sent"]
    assert len(walk.clicks) == 3
    assert not any("boards.greenhouse.io/acme/jobs/8095311?gh_jid" in url for url in walk.clicks)


def test_a_posting_already_in_the_history_is_skipped():
    walk = Walk(
        _jobs(3),
        history=lambda *_a, **_k: ["https://boards.greenhouse.io/acme/jobs/8095311?gh_jid=8095311"],
    )
    assert walk.run(max_jobs=5) == ["sent", "sent"]
    assert not any(url.endswith("/8095311") for url in walk.clicks)


def test_a_live_walk_refuses_to_start_when_the_history_cannot_be_read():
    """ "Could not tell" must never read as "nothing sent" — the dedup used to fail OPEN."""

    def boom(*_a, **_k):
        raise RuntimeError("upstream connect error / 503")

    walk = Walk(_jobs(3), history=boom)
    with pytest.raises(SystemExit, match="LIVE REFUSED"):
        walk.run(max_jobs=2)
    assert walk.clicks == []
    # A dry-run may go on without it: it sends nothing.
    assert Walk(_jobs(2), history=boom).run(max_jobs=1, live=False) == ["dry"]


def test_a_sent_application_that_could_not_be_saved_is_still_one_application():
    def save(**_kw):
        raise RuntimeError("PGRST: connection reset")

    walk = Walk(_jobs(4), save=save)
    assert walk.run(max_jobs=1) == ["sent"]
    assert len(walk.clicks) == 1
    # Nothing in the applications table says so — the open hand-back does.
    assert walk.handbacks == [("j0", "sent_unrecorded")]


def test_a_knockout_leaves_the_pool_so_it_is_not_filled_again_tomorrow():
    async def knocked(_form):
        return ["Are you based within a commutable distance of Dallas? → No"]

    walk = Walk(_jobs(2), knockouts=knocked)
    assert walk.run(max_jobs=1) == ["knockout", "knockout"]
    assert walk.clicks == []
    assert len(walk.retired) == 2
