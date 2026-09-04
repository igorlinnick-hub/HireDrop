from abc import ABC, abstractmethod


class JobPlatform(ABC):
    name = ""
    display_name = ""
    requires_credentials = False  # True = needs stored platform credentials
    # Short user-facing reason when the source is KNOWN dead server-side (e.g. the site
    # now requires a real browser). The router skips these before calling scrape() and
    # surfaces the reason, instead of letting them contribute a silent zero. A platform
    # that quietly returns [] is indistinguishable from a broken one — that is exactly
    # how the `jobspy`/`python-jobspy` package mix-up (#113) hid in prod for weeks.
    unavailable_reason: str | None = None

    @abstractmethod
    def scrape(self, keywords=None, location="remote", max_results=25):
        """Scrape jobs and return list of normalized job dicts.

        Each dict must have:
            title, company, link, date, platform, location, job_type, tags, description
        """
        pass
