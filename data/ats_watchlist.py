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
    ("cityblock", "greenhouse"),      # healthcare
    ("devoted", "greenhouse"),        # healthcare
    ("oura", "greenhouse"),           # wellness
    ("calm", "greenhouse"),           # wellness
    ("noom", "greenhouse"),           # wellness
    # --- Greenhouse (added 2026-07-13; token-validated live, all zero-touch reCAPTCHA v3) ---
    ("anthropic", "greenhouse"),      # 405 open roles
    ("cloudflare", "greenhouse"),     # 260
    ("reddit", "greenhouse"),         # 191
    ("scaleai", "greenhouse"),        # 183
    ("affirm", "greenhouse"),         # fintech, 179
    ("twilio", "greenhouse"),         # 153
    ("gusto", "greenhouse"),          # fintech/HR
    ("chime", "greenhouse"),          # fintech
    ("marqeta", "greenhouse"),        # fintech
    ("webflow", "greenhouse"),
    ("mavenclinic", "greenhouse"),    # health/wellness
    ("modernhealth", "greenhouse"),   # mental health
    # --- Greenhouse (added 2026-07-13 batch 2; diversified niches, token-validated) ---
    ("postman", "greenhouse"),        # dev tools, 119
    ("faire", "greenhouse"),          # marketplace, 75
    ("carta", "greenhouse"),          # fintech, 59
    ("mercury", "greenhouse"),        # fintech, 58
    ("tanium", "greenhouse"),         # security, 44
    ("amplitude", "greenhouse"),      # analytics, 41
    ("omadahealth", "greenhouse"),    # health/wellness, 24
    ("glossier", "greenhouse"),       # consumer/beauty marketing, 19
    ("papa", "greenhouse"),           # health/care
    # --- Lever ---
    ("spotify", "lever"),             # media/consumer, 111

    ("shieldai", "lever"),
    ("matchgroup", "lever"),
    ("Huckleberrylabs", "lever"),
    ("ro", "lever"),                  # telehealth
    ("plaid", "lever"),
    ("brex", "lever"),
    ("sesame", "lever"),              # telehealth
    ("cerebral", "lever"),            # mental health
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
]
