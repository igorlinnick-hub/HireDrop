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
]
