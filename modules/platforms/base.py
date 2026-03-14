from abc import ABC, abstractmethod


class JobPlatform(ABC):
    name = ""
    display_name = ""

    @abstractmethod
    def scrape(self, keywords=None, location="remote", max_results=25):
        """Scrape jobs and return list of normalized job dicts.

        Each dict must have:
            title, company, link, date, platform, location, job_type, tags, description
        """
        pass
