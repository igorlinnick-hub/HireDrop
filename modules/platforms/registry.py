from modules.platforms.indeed import IndeedPlatform
from modules.platforms.remoteok import RemoteOKPlatform
from modules.platforms.wellfound import WellfoundPlatform

PLATFORMS = {
    "remoteok": RemoteOKPlatform,
    "indeed": IndeedPlatform,
    "wellfound": WellfoundPlatform,
}


def get_enabled_platforms(profile):
    enabled = profile.get("platforms", ["remoteok"])
    return [PLATFORMS[p]() for p in enabled if p in PLATFORMS]
