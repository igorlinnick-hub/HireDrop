from modules.platforms.base import JobPlatform


class HiredPlatform(JobPlatform):
    name = "hired"
    display_name = "Hired"

    def scrape(self, keywords=None, location="remote", max_results=25):
        # TODO: implement Hired scraper (paid platform)
        return []
