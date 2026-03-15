from modules.platforms.base import JobPlatform


class ZipRecruiterPlatform(JobPlatform):
    name = "ziprecruiter"
    display_name = "ZipRecruiter"

    def scrape(self, keywords=None, location="remote", max_results=25):
        # TODO: implement ZipRecruiter scraper
        return []
