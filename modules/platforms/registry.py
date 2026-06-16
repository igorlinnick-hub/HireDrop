from modules.platforms.craigslist import CraigslistPlatform
from modules.platforms.indeed import IndeedPlatform
from modules.platforms.jobspy_platform import (
    GlassdoorPlatform,
    GoogleJobsPlatform,
    LinkedInPlatform,
    ZipRecruiterPlatform,
)
from modules.platforms.remoteok import RemoteOKPlatform
from modules.platforms.wellfound import WellfoundPlatform

PLATFORMS = {
    "remoteok": RemoteOKPlatform,
    "indeed": IndeedPlatform,
    "wellfound": WellfoundPlatform,
    "linkedin": LinkedInPlatform,
    "glassdoor": GlassdoorPlatform,
    "ziprecruiter": ZipRecruiterPlatform,
    "google": GoogleJobsPlatform,
    "craigslist": CraigslistPlatform,
}


def get_enabled_platforms(profile):
    enabled = profile.get("platforms", ["remoteok"])
    return [PLATFORMS[p]() for p in enabled if p in PLATFORMS]
