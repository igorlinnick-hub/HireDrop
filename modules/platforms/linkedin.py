from modules.platforms.base import JobPlatform


class LinkedInPlatform(JobPlatform):
    name = "linkedin"
    display_name = "LinkedIn"

    def scrape(self, keywords=None, location="remote", max_results=25):
        # TODO: implement LinkedIn scraper
        return []
