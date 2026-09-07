"""Seed watchlist of company ATS board tokens for direct-source discovery (ats_boards.py).

There is no global GH/Lever search — discovery = query these companies' public board APIs.
This is a curated STARTER set; grow it over time (public GH/Lever company lists, plus
self-seeding from companies already seen). Each entry: (token, platform).
Invalid/renamed tokens are skipped gracefully by the fetchers (404 -> []).
"""

SEED_WATCHLIST: list[tuple[str, str]] = [
    # --- Greenhouse ---
    ("airtable", "greenhouse"),
    ("stripe", "greenhouse"),
    ("figma", "greenhouse"),
    ("databricks", "greenhouse"),
    ("gitlab", "greenhouse"),
    ("discord", "greenhouse"),
    ("robinhood", "greenhouse"),
    ("coinbase", "greenhouse"),
    ("benchling", "greenhouse"),
    ("whoop", "greenhouse"),
    ("hims", "greenhouse"),
    ("cityblock", "greenhouse"),  # healthcare
    ("devoted", "greenhouse"),  # healthcare
    ("oura", "greenhouse"),  # wellness
    ("calm", "greenhouse"),  # wellness
    ("noom", "greenhouse"),  # wellness
    # --- Greenhouse (added 2026-07-13; token-validated live, all zero-touch reCAPTCHA v3) ---
    ("anthropic", "greenhouse"),  # 405 open roles
    ("cloudflare", "greenhouse"),  # 260
    ("reddit", "greenhouse"),  # 191
    ("scaleai", "greenhouse"),  # 183
    ("affirm", "greenhouse"),  # fintech, 179
    ("twilio", "greenhouse"),  # 153
    ("gusto", "greenhouse"),  # fintech/HR
    ("chime", "greenhouse"),  # fintech
    ("marqeta", "greenhouse"),  # fintech
    ("webflow", "greenhouse"),
    ("mavenclinic", "greenhouse"),  # health/wellness
    ("modernhealth", "greenhouse"),  # mental health
    # --- Greenhouse (added 2026-07-13 batch 2; diversified niches, token-validated) ---
    ("postman", "greenhouse"),  # dev tools, 119
    ("faire", "greenhouse"),  # marketplace, 75
    ("carta", "greenhouse"),  # fintech, 59
    ("mercury", "greenhouse"),  # fintech, 58
    ("tanium", "greenhouse"),  # security, 44
    ("amplitude", "greenhouse"),  # analytics, 41
    ("omadahealth", "greenhouse"),  # health/wellness, 24
    ("glossier", "greenhouse"),  # consumer/beauty marketing, 19
    ("papa", "greenhouse"),  # health/care
    # --- Lever ---
    ("spotify", "lever"),  # media/consumer, 111
    ("shieldai", "lever"),
    ("matchgroup", "lever"),
    ("Huckleberrylabs", "lever"),
    ("ro", "lever"),  # telehealth
    ("plaid", "lever"),
    ("brex", "lever"),
    ("sesame", "lever"),  # telehealth
    ("cerebral", "lever"),  # mental health
    # --- added 2026-07-29: token-validated live against public board APIs
    #     (script-verified 200 + >0 postings; job counts at verification time) ---
    ("datadog", "greenhouse"),  # 424
    ("mongodb", "greenhouse"),  # 399
    ("onemedical", "greenhouse"),  # 350
    ("hellofresh", "greenhouse"),  # 340
    ("charliehealth", "greenhouse"),  # 316
    ("braze", "greenhouse"),  # 236
    ("adyen", "greenhouse"),  # 224
    ("roblox", "greenhouse"),  # 220
    ("fivetran", "greenhouse"),  # 205
    ("betterhelp", "greenhouse"),  # 202
    ("pinterest", "greenhouse"),  # 202
    ("airbnb", "greenhouse"),  # 195
    ("sezzle", "greenhouse"),  # 170
    ("clickhouse", "greenhouse"),  # 168
    ("riotgames", "greenhouse"),  # 163
    ("lyft", "greenhouse"),  # 160
    ("klaviyo", "greenhouse"),  # 151
    ("epicgames", "greenhouse"),  # 139
    ("grafanalabs", "greenhouse"),  # 136
    ("payoneer", "greenhouse"),  # 130
    ("instacart", "greenhouse"),  # 125
    ("mozilla", "greenhouse"),  # 84
    ("misfitsmarket", "greenhouse"),  # 82
    ("vercel", "greenhouse"),  # 77
    ("similarweb", "greenhouse"),  # 67
    ("twitch", "greenhouse"),  # 67
    ("hightouch", "greenhouse"),  # 66
    ("classpass", "greenhouse"),  # 66
    ("duolingo", "greenhouse"),  # 64
    ("sofi", "greenhouse"),  # 59
    ("temporaltechnologies", "greenhouse"),  # 57
    ("checkr", "greenhouse"),  # 53
    ("cision", "greenhouse"),  # 52
    ("mixpanel", "greenhouse"),  # 43
    ("neo4j", "greenhouse"),  # 42
    ("snorkelai", "greenhouse"),  # 42
    ("homechef", "greenhouse"),  # 40
    ("betterment", "greenhouse"),  # 39
    ("later", "greenhouse"),  # 39
    ("launchdarkly", "greenhouse"),  # 34
    ("stockx", "greenhouse"),  # 34
    ("cockroachlabs", "greenhouse"),  # 32
    ("earnin", "greenhouse"),  # 32
    ("tia", "greenhouse"),  # 31
    ("daybreakhealth", "greenhouse"),  # 30
    ("turing", "greenhouse"),  # 28
    ("yext", "greenhouse"),  # 27
    ("customerio", "greenhouse"),  # 25
    ("iterable", "greenhouse"),  # 23
    ("mindbody", "greenhouse"),  # 21
    ("alloy", "greenhouse"),  # 21
    ("hootsuite", "greenhouse"),  # 19
    ("brandwatch", "greenhouse"),  # 19
    ("airship", "greenhouse"),  # 19
    ("hazel", "greenhouse"),  # 18
    ("thrivemarket", "greenhouse"),  # 18
    ("coursera", "greenhouse"),  # 17
    ("upgrade", "greenhouse"),  # 17
    ("wikimedia", "greenhouse"),  # 17
    ("squarespace", "greenhouse"),  # 16
    ("nextdoor", "greenhouse"),  # 16
    ("pelago", "greenhouse"),  # 16
    ("insider", "greenhouse"),  # 15
    ("amwell", "greenhouse"),  # 14
    ("muckrack", "greenhouse"),  # 13
    ("taskrabbit", "greenhouse"),  # 12
    ("galileo", "greenhouse"),  # 11
    ("udemy", "greenhouse"),  # 10
    ("talkspace", "greenhouse"),  # 10
    ("lithic", "greenhouse"),  # 9
    ("wellthy", "greenhouse"),  # 9
    ("emplifi", "greenhouse"),  # 9
    ("planetscale", "greenhouse"),  # 8
    ("circleci", "greenhouse"),  # 7
    ("labelbox", "greenhouse"),  # 7
    ("invisible", "greenhouse"),  # 7
    ("forward", "greenhouse"),  # 6
    ("sproutsocial", "greenhouse"),  # 6
    ("postscript", "greenhouse"),  # 6
    ("netlify", "greenhouse"),  # 5
    ("offerup", "greenhouse"),  # 5
    ("stabilityai", "greenhouse"),  # 5
    ("gatherup", "greenhouse"),  # 3
    ("hungryroot", "greenhouse"),  # 3
    ("masterclass", "greenhouse"),  # 2
    ("cameo", "greenhouse"),  # 2
    ("community", "greenhouse"),  # 1
    ("medium", "greenhouse"),  # 1
    ("revel", "greenhouse"),  # 1
    ("palantir", "lever"),
    ("outreach", "lever"),
    ("zoox", "lever"),
    ("binance", "lever"),
    ("anchorage", "lever"),
    ("verifiable", "lever"),
    # --- Lever MARKETING/consumer/media (added 2026-07-29; script-verified live: board 200
    # AND has marketing/social roles — the old lever set was tech/fintech-only, so Igor's
    # marketing keywords yielded 0 Lever inventory. These give Lever real supply to test). ---
    ("wpromote", "lever"),  # performance-marketing agency, 15 mktg roles
    ("superside", "lever"),  # creative/design-as-a-service, 11
    ("theathletic", "lever"),  # sports media, 5
    ("rover", "lever"),  # consumer marketplace, 5
    ("gopuff", "lever"),  # delivery/consumer, 4
    ("morningbrew", "lever"),  # media/newsletter, 3
    # --- Ashby (added 2026-07-31; 5th platform, script-verified live: board 200 AND has
    # marketing/social roles. Ashby = guest-apply ATS like GH, api.ashbyhq.com posting-api.
    # 40 orgs, 427 marketing roles total; ordered by marketing yield). ---
    ("elevenlabs", "ashby"),  # 35 mktg
    ("supabase", "ashby"),  # 29
    ("suno", "ashby"),  # 21
    ("eightsleep", "ashby"),  # 12
    ("gamma", "ashby"),  # 11
    ("harvey", "ashby"),
    ("perplexity", "ashby"),
    ("sierra", "ashby"),
    ("vanta", "ashby"),
    ("notion", "ashby"),
    ("cursor", "ashby"),
    ("ramp", "ashby"),
    ("cohere", "ashby"),
    ("column", "ashby"),
    ("mercor", "ashby"),
    ("decagon", "ashby"),
    ("baseten", "ashby"),
    ("temporal", "ashby"),
    ("render", "ashby"),
    ("zip", "ashby"),
    ("warp", "ashby"),
    ("photoroom", "ashby"),
    ("writer", "ashby"),
    ("found", "ashby"),
    ("workos", "ashby"),
    ("linear", "ashby"),
    ("posthog", "ashby"),
    ("modal", "ashby"),
    ("neon", "ashby"),
    ("pika", "ashby"),
    ("krea", "ashby"),
    ("hedra", "ashby"),
    ("semgrep", "ashby"),
    ("infisical", "ashby"),
    ("watershed", "ashby"),
    ("runway", "ashby"),
    ("resend", "ashby"),
    ("unit", "ashby"),
    ("persona", "ashby"),
    ("ideogram", "ashby"),
    # --- Workday (added 2026-07-31; DISCOVERY-ONLY, big US enterprise. Token = tenant|dc|site;
    # apply is account-gated multi-step (not yet auto-filled) so these feed the pool for
    # "scrapeable" coverage but the extension never walks them. Script-verified live: cxs API
    # 200 + marketing>0. Ordered by marketing yield). ---
    ("nvidia|wd5|NVIDIAExternalCareerSite", "workday"),  # 780 mktg
    ("adobe|wd5|external_experienced", "workday"),  # 701
    ("salesforce|wd12|External_Career_Site", "workday"),  # 458
    ("mastercard|wd1|CorporateCareers", "workday"),  # 307
    ("hp|wd5|ExternalCareerSite", "workday"),  # 227
    ("paypal|wd1|jobs", "workday"),  # 112
    ("target|wd5|targetcareers", "workday"),  # 91
    ("workday|wd5|Workday", "workday"),  # 90
    ("cvshealth|wd1|CVS_Health_Careers", "workday"),  # 79
    # --- added 2026-09-06: NON-TECH VERTICALS. The list above is SaaS/fintech/AI almost
    #     end to end, so a healthcare-marketing profile drew 3 Greenhouse candidates for a
    #     whole sweep — the boards simply had no such work on them. These were validated the
    #     same way as the earlier batches (board API 200 + counted live postings) and picked
    #     for INVENTORY IN THE VERTICAL, not for name recognition. Counts at verification:
    #     h=healthcare/clinical, m=marketing/comms, o=front-office ops, s=hospitality/food.
    ("doordashusa", "greenhouse"),  # 456 · m30 s30 o16 — delivery/ops at scale
    ("toast", "greenhouse"),  # 315 · restaurant tech, m9
    ("oscar", "greenhouse"),  # 283 · h39 m9 o15 — health insurer
    ("flexport", "greenhouse"),  # 174 · o23 — logistics ops
    ("opentable", "greenhouse"),  # 101 · m8 — restaurants
    ("vaynermedia", "greenhouse"),  # 84 · m16 — marketing agency, the densest mktg board here
    ("sweetgreen", "greenhouse"),  # 58 · s35 — restaurant field + support roles
    ("strivehealth", "greenhouse"),  # 58 · h36 — kidney care
    ("instawork", "greenhouse"),  # 58 · hourly-staffing marketplace
    ("zocdoc", "greenhouse"),  # 52 · h3 m5 o2
    ("attentive", "greenhouse"),  # 37 · m3 — retention marketing
    ("movableink", "greenhouse"),  # 34 · m1
    ("komodohealth", "greenhouse"),  # 34 · h1
    ("flatironhealth", "greenhouse"),  # 32 · h1 m4 — oncology
    ("khanacademy", "greenhouse"),  # 23 · o5 m2 — education
    ("voxmedia", "greenhouse"),  # 17 · m3 — media/editorial
    ("honor", "greenhouse"),  # 15 · h1 — home care
    ("kasa", "greenhouse"),  # 15 · s2 — hospitality operator
    ("parsleyhealth", "greenhouse"),  # 10 · h7 — primary care
    ("folxhealth", "greenhouse"),  # 7 · h5 — telehealth
    ("buzzfeed", "greenhouse"),  # 6 · m1 — media
    ("commure", "ashby"),  # 76 · h3 m5 o2 — health systems software
    ("abridge", "ashby"),  # 40 · h2 — clinical AI
    ("tennr", "ashby"),  # 21 · h2 — healthcare referrals
    ("newsela", "greenhouse"),  # 16 · education
    ("outschool", "greenhouse"),  # 4 · education
]


# --- Vertical tags -----------------------------------------------------------------
# A sweep collects far more postings than its cap can return, so the ORDER boards are
# fetched in decides what a user actually sees — and the order was "whatever position the
# board holds in the list above". That made the tail dead weight: the 24 non-tech boards
# added 2026-09-06 landed 0 of 160 results on Igor's own keywords, because 100+ SaaS
# boards ahead of them had already filled the cap with marketing roles at AI companies.
#
# So tag the boards whose inventory is concentrated in a vertical, and let discover_ats
# put the matching ones FIRST when the user's keywords point that way. Untagged boards
# keep their existing order, so a tech search behaves exactly as before.
BOARD_VERTICALS: dict[str, tuple[str, ...]] = {
    # healthcare / clinical / wellness
    "oscar": ("health",),
    "strivehealth": ("health",),
    "zocdoc": ("health",),
    "parsleyhealth": ("health",),
    "folxhealth": ("health",),
    "komodohealth": ("health",),
    "flatironhealth": ("health",),
    "honor": ("health",),
    "commure": ("health",),
    "abridge": ("health",),
    "tennr": ("health",),
    "mavenclinic": ("health",),
    "modernhealth": ("health",),
    "charliehealth": ("health",),
    "betterhelp": ("health",),
    "talkspace": ("health",),
    "onemedical": ("health",),
    "omadahealth": ("health",),
    "cityblock": ("health",),
    "devoted": ("health",),
    "amwell": ("health",),
    "wellthy": ("health",),
    "hazel": ("health",),
    "tia": ("health",),
    "daybreakhealth": ("health",),
    "papa": ("health",),
    "hims": ("health",),
    "ro": ("health",),
    "sesame": ("health",),
    "cerebral": ("health",),
    "galileo": ("health",),
    "forward": ("health",),
    # hospitality / food service / physical operations
    "sweetgreen": ("hospitality",),
    "kasa": ("hospitality",),
    "toast": ("hospitality",),
    "opentable": ("hospitality",),
    "doordashusa": ("hospitality", "ops"),
    "instawork": ("hospitality", "ops"),
    "classpass": ("hospitality", "health"),
    "mindbody": ("hospitality", "health"),
    "hellofresh": ("hospitality",),
    "homechef": ("hospitality",),
    "hungryroot": ("hospitality",),
    "thrivemarket": ("hospitality",),
    "misfitsmarket": ("hospitality",),
    # marketing agencies, media, comms
    "vaynermedia": ("agency",),
    "wpromote": ("agency",),
    "superside": ("agency",),
    "attentive": ("agency",),
    "movableink": ("agency",),
    "sproutsocial": ("agency",),
    "hootsuite": ("agency",),
    "later": ("agency",),
    "brandwatch": ("agency",),
    "emplifi": ("agency",),
    "muckrack": ("agency",),
    "cision": ("agency",),
    "voxmedia": ("media",),
    "buzzfeed": ("media",),
    "theathletic": ("media",),
    "morningbrew": ("media",),
    "medium": ("media",),
    # education
    "khanacademy": ("education",),
    "coursera": ("education",),
    "udemy": ("education",),
    "duolingo": ("education",),
    "masterclass": ("education",),
    "outschool": ("education",),
    "newsela": ("education",),
    # logistics / field & back-office operations
    "flexport": ("ops",),
    "taskrabbit": ("ops",),
    "rover": ("ops",),
    "gopuff": ("ops",),
    "instacart": ("ops",),
}

# What a user's keywords have to look like for a vertical's boards to jump the queue.
# Matched as substrings against the lowercased keyword phrases.
VERTICAL_HINTS: dict[str, tuple[str, ...]] = {
    "health": (
        "health",
        "clinic",
        "nurse",
        "rn",
        "patient",
        "medical",
        "care",
        "therap",
        "behavioral",
        "dental",
        "pharma",
        "wellness",
        "hospital",
    ),
    "hospitality": (
        "hotel",
        "restaurant",
        "hospitality",
        "guest",
        "food",
        "barista",
        "server",
        "culinary",
        "resort",
        "housekeep",
        "kitchen",
        "cafe",
        "fitness",
        "gym",
        "spa",
        "salon",
    ),
    "agency": (
        "marketing",
        "brand",
        "advertis",
        "agency",
        "social media",
        "seo",
        "copywrit",
        "public relations",
        "communications",
        "content",
    ),
    "media": ("media", "editorial", "journalis", "writer", "content", "publish", "video"),
    "education": (
        "teacher",
        "education",
        "curriculum",
        "instructional",
        "tutor",
        "school",
        "learning",
    ),
    "ops": (
        "operations",
        "logistics",
        "warehouse",
        "dispatch",
        "supply chain",
        "administrative",
        "front desk",
        "receptionist",
        "coordinator",
        "driver",
    ),
}


def prioritized_boards(
    companies: list[tuple[str, str]], keywords: list[str] | None
) -> list[tuple[str, str]]:
    """Boards whose vertical matches the keywords first; everything else in list order.

    Stable: within each half the original order is preserved, so this only ever promotes
    relevant boards — it never reshuffles the curated list.
    """
    if not keywords:
        return list(companies)
    text = " ".join(k.lower() for k in keywords if k)
    wanted = {v for v, hints in VERTICAL_HINTS.items() if any(h in text for h in hints)}
    if not wanted:
        return list(companies)
    front, back = [], []
    for token, platform in companies:
        tags = BOARD_VERTICALS.get(token.lower(), ())
        (front if any(t in wanted for t in tags) else back).append((token, platform))
    return front + back
