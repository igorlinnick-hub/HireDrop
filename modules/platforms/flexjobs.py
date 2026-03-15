from modules.platforms.base import JobPlatform


class FlexJobsPlatform(JobPlatform):
    name = "flexjobs"
    display_name = "FlexJobs"

    def scrape(self, keywords=None, location="remote", max_results=25):
        # TODO: implement FlexJobs scraper (paid platform — $15/mo)
        return []
