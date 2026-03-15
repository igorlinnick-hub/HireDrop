from modules.platforms.base import JobPlatform


class ToptalPlatform(JobPlatform):
    name = "toptal"
    display_name = "Toptal"

    def scrape(self, keywords=None, location="remote", max_results=25):
        # TODO: implement Toptal scraper (paid platform)
        return []
