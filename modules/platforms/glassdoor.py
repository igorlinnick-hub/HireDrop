from modules.platforms.base import JobPlatform


class GlassdoorPlatform(JobPlatform):
    name = "glassdoor"
    display_name = "Glassdoor"

    def scrape(self, keywords=None, location="remote", max_results=25):
        # TODO: implement Glassdoor scraper
        return []
