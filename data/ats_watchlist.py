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
    # --- Lever ---
    ("shieldai", "lever"),
    ("matchgroup", "lever"),
    ("Huckleberrylabs", "lever"),
    ("ro", "lever"),                  # telehealth
    ("plaid", "lever"),
    ("brex", "lever"),
    ("sesame", "lever"),              # telehealth
    ("cerebral", "lever"),            # mental health
]
