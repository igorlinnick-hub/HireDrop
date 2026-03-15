from modules.platforms.remoteok import RemoteOKPlatform
from modules.platforms.indeed import IndeedPlatform
from modules.platforms.wellfound import WellfoundPlatform

from modules.platforms.glassdoor import GlassdoorPlatform
from modules.platforms.ziprecruiter import ZipRecruiterPlatform
from modules.platforms.google_jobs import GoogleJobsPlatform
from modules.platforms.dice import DicePlatform
from modules.platforms.toptal import ToptalPlatform
from modules.platforms.hired import HiredPlatform
from modules.platforms.flexjobs import FlexJobsPlatform

PLATFORMS = {
    "remoteok": RemoteOKPlatform,
    "indeed": IndeedPlatform,

    "wellfound": WellfoundPlatform,
    "glassdoor": GlassdoorPlatform,
    "ziprecruiter": ZipRecruiterPlatform,
    "google_jobs": GoogleJobsPlatform,
    "dice": DicePlatform,
    "toptal": ToptalPlatform,
    "hired": HiredPlatform,
    "flexjobs": FlexJobsPlatform,
}


def get_enabled_platforms(profile):
    enabled = profile.get("platforms", ["remoteok"])
    return [PLATFORMS[p]() for p in enabled if p in PLATFORMS]
