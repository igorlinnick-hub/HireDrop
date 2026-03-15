from modules.platforms.base import JobPlatform


class GoogleJobsPlatform(JobPlatform):
    name = "google_jobs"
    display_name = "Google Jobs"

    def scrape(self, keywords=None, location="remote", max_results=25):
        # TODO: implement Google Jobs scraper
        return []
