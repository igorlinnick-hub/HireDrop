from modules.platforms.base import JobPlatform


class DicePlatform(JobPlatform):
    name = "dice"
    display_name = "Dice"

    def scrape(self, keywords=None, location="remote", max_results=25):
        # TODO: implement Dice scraper
        return []
