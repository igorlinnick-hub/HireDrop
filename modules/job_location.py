"""Does a posting's location fit the user's? — for the Tap deck.

The pool's `location` is free text from 200+ boards. A sample from one live pool:
"Foster City, CA" / "Remote - Ontario, Canada" / "Hybrid - San Francisco, California" /
"New York City, NY | Seattle, WA; San Francisco, CA" / "Sweden" / "Austin" / "Florida -
Remote". There are no coordinates anywhere in the backend, so a miles radius is not
honestly computable here — the radius picker steers the NATIVE searches (Indeed URL
params); this module answers the only question the deck can answer truthfully: does the
text say this job could be done from the user's place (same city, same state, or remote)?

Three-way verdict, same philosophy as job_type: silence must stay distinct from a stated
mismatch. A row with no location PASSES (the whole pre-existing pool would vanish
otherwise); a row that plainly names somewhere else does not.
"""

import re

# "Remote" appearing anywhere in the location is treated as workable-from-home. A scoped
# remote ("Remote - Ontario, Canada", "Remote (Bulgaria)") is usually a legal-entity
# constraint — but parsing residency law out of free text is guesswork, and the swipe is
# the human decision anyway. We only require the USER'S COUNTRY to not be contradicted:
# a US user passes "Remote US" and bare "Remote", and fails "Remote (Bulgaria)".
_REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b|\bwfh\b|\banywhere\b", re.I)

_US_HINT_RE = re.compile(r"\bus\b|\busa\b|\bunited states\b|\bamericas?\b|\bnorth america\b", re.I)

# Countries that plainly are not the US — enough to catch the live pool's shapes
# (Sweden, Singapore, Colombia, "Tel Aviv, Israel", "India - Bangalore"). Not a gazetteer:
# an unlisted foreign city simply falls through to the city/state check and fails there.
_NON_US_RE = re.compile(
    r"\b(canada|mexico|argentina|colombia|brazil|bulgaria|ireland|united kingdom|uk|"
    r"england|germany|france|spain|portugal|poland|romania|sweden|norway|denmark|"
    r"finland|netherlands|belgium|switzerland|austria|italy|greece|turkey|israel|"
    r"india|pakistan|china|japan|korea|singapore|philippines|vietnam|thailand|"
    r"indonesia|malaysia|australia|new zealand|nigeria|kenya|egypt|south africa|"
    r"ukraine|georgia \(country\)|armenia|kazakhstan)\b",
    re.I,
)

# Foreign HUB CITIES that show up in board locations WITHOUT their country ("Bengaluru",
# "Remote - Pune", "Toronto"). The country regex above never sees them, which is exactly
# how a "remote" search surfaced India: a scoped-remote row naming only the city sailed
# through the remote branch below. Same philosophy as _NON_US_RE — top offenders, not a
# gazetteer. US towns sharing a name (Dublin OH, Melbourne FL, Athens GA…) are protected
# by the state-hint check in names_foreign_country, not by omission here.
_NON_US_CITY_RE = re.compile(
    r"\b(bengaluru|bangalore|hyderabad|pune|mumbai|delhi|chennai|noida|gurgaon|gurugram|"
    r"kolkata|ahmedabad|toronto|vancouver|montreal|ottawa|london|manchester|edinburgh|"
    r"berlin|munich|hamburg|paris|amsterdam|dublin|madrid|barcelona|lisbon|warsaw|"
    r"krakow|kraków|prague|budapest|bucharest|sofia|athens|stockholm|oslo|copenhagen|"
    r"helsinki|zurich|geneva|vienna|milan|rome|istanbul|tel aviv|dubai|tokyo|osaka|"
    r"seoul|beijing|shanghai|shenzhen|taipei|hong kong|manila|jakarta|kuala lumpur|"
    r"bangkok|hanoi|ho chi minh|sydney|melbourne|brisbane|auckland|wellington|"
    r"s[ãa]o paulo|rio de janeiro|buenos aires|santiago|bogot[áa]|lima|mexico city|"
    r"monterrey|guadalajara|lagos|nairobi|cape town|johannesburg|kyiv|kiev|tbilisi|"
    r"yerevan|almaty|tashkent)\b",
    re.I,
)

_STATE_CODES = {
    "al": "alabama",
    "ak": "alaska",
    "az": "arizona",
    "ar": "arkansas",
    "ca": "california",
    "co": "colorado",
    "ct": "connecticut",
    "de": "delaware",
    "fl": "florida",
    "ga": "georgia",
    "hi": "hawaii",
    "id": "idaho",
    "il": "illinois",
    "in": "indiana",
    "ia": "iowa",
    "ks": "kansas",
    "ky": "kentucky",
    "la": "louisiana",
    "me": "maine",
    "md": "maryland",
    "ma": "massachusetts",
    "mi": "michigan",
    "mn": "minnesota",
    "ms": "mississippi",
    "mo": "missouri",
    "mt": "montana",
    "ne": "nebraska",
    "nv": "nevada",
    "nh": "new hampshire",
    "nj": "new jersey",
    "nm": "new mexico",
    "ny": "new york",
    "nc": "north carolina",
    "nd": "north dakota",
    "oh": "ohio",
    "ok": "oklahoma",
    "or": "oregon",
    "pa": "pennsylvania",
    "ri": "rhode island",
    "sc": "south carolina",
    "sd": "south dakota",
    "tn": "tennessee",
    "tx": "texas",
    "ut": "utah",
    "vt": "vermont",
    "va": "virginia",
    "wa": "washington",
    "wv": "west virginia",
    "wi": "wisconsin",
    "wy": "wyoming",
    "dc": "district of columbia",
}
_NAME_TO_CODE = {v: k for k, v in _STATE_CODES.items()}

# "City, ST" is the canonical US shape — a state code right after a comma is a US hint
# strong enough to clear a foreign-named town (Dublin, OH / Melbourne, FL / Athens, GA).
# Bare 2-letter matching would be noise ("Remote in India" contains "in"), so the comma
# anchors it. Full state names count too ("Albuquerque, New Mexico" must not read as
# Mexico).
_US_STATE_CODE_HINT_RE = re.compile(r",\s*(" + "|".join(_STATE_CODES) + r")\b", re.I)
_US_STATE_NAME_HINT_RE = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in _NAME_TO_CODE) + r")\b", re.I
)


def names_foreign_country(row_location: str) -> bool:
    """Does this free-text location plainly place the job OUTSIDE the US?

    The country-level question every US-serving surface can ask without knowing the
    user's city: harvest (don't pool it), the listing (don't show it), the deck/queue
    (don't swipe/apply it). Conservative on purpose — an unrecognized string returns
    False and flows through to the finer city/state logic or passes as unknown; this
    only fires when the text names a non-US country or a known foreign hub city and
    gives the US no mention at all.
    """
    low = " " + (row_location or "").lower() + " "
    if _US_HINT_RE.search(low) or _US_STATE_CODE_HINT_RE.search(low):
        return False
    if _US_STATE_NAME_HINT_RE.search(low):
        return False
    return bool(_NON_US_RE.search(low) or _NON_US_CITY_RE.search(low))


def parse_user_location(location: str) -> dict:
    """ "Miami, Florida, US" -> {city: "miami", state_code: "fl", state_name: "florida"}.

    Best-effort on the profile's own free text; missing pieces stay None and simply
    widen what passes (no city = state-level matching), never narrow it.
    """
    parts = [p.strip().lower() for p in re.split(r"[,/|]", location or "") if p.strip()]
    city = state_code = state_name = None
    for part in parts:
        bare = re.sub(r"[^a-z ]", "", part).strip()
        if bare in _NAME_TO_CODE:
            state_name, state_code = bare, _NAME_TO_CODE[bare]
        elif bare in _STATE_CODES:
            state_code, state_name = bare, _STATE_CODES[bare]
        # Coarse enum values are not cities. "remote" IS kept as a pseudo-city on
        # purpose — it turns the deck filter on, and the verdict's remote branch
        # handles it before any city compare — but "europe" as a city made every
        # placeable row on a europe profile read "elsewhere" and emptied the deck.
        elif bare not in ("us", "usa", "united states", "europe", "anywhere", "worldwide") and (
            city is None
        ):
            city = bare
    return {"city": city, "state_code": state_code, "state_name": state_name}


def location_verdict(row_location: str, user: dict) -> str:
    """ "fits" | "elsewhere" | "unknown" for one pool row against a parsed user location.

    unknown is a real answer: the deck PASSES it (hiding every legacy row to prove a
    point is the worse failure — the job_type lesson) but counts it separately, so an
    uninformed filter is never mistaken for a broken one.
    """
    text = (row_location or "").strip()
    if not text:
        return "unknown"
    low = " " + text.lower() + " "

    if _REMOTE_RE.search(low):
        # Remote fits unless it names a non-US scope and gives the US no mention.
        # A scope can be a country OR a bare foreign hub city ("Remote - Bengaluru")
        # — the city shape is how India leaked through a "remote" search (09-21).
        # names_foreign_country carries the "City, ST" protection, so Remote -
        # Melbourne, FL stays a fit while Remote - Melbourne reads as Australia.
        if names_foreign_country(text):
            return "elsewhere"
        return "fits"

    city, code, name = user.get("city"), user.get("state_code"), user.get("state_name")
    if city and re.search(rf"\b{re.escape(city)}\b", low):
        return "fits"
    # Same state: honest for an on-site row only in the sense of "possibly in range" —
    # without coordinates this is the finest honest granularity, and the swipe decides.
    if name and re.search(rf"\b{re.escape(name)}\b", low):
        return "fits"
    if code and re.search(rf"\b{re.escape(code)}\b", low):
        return "fits"

    # Plainly somewhere else (a named foreign country, or US text that matched nothing
    # of the user's) — but only call it elsewhere when the text really names a place:
    # shapes like "Hybrid" carry no geography at all and stay unknown.
    if _NON_US_RE.search(low):
        return "elsewhere"
    # Bare work-mode words carry no geography at all — they must stay unknown, not
    # become a miss (the trailing-space padding above means .strip() here, not equality).
    if low.strip() in ("hybrid", "on-site", "onsite", "in office", "in-office", "office"):
        return "unknown"
    if re.search(r"[a-z]", low) and (
        re.search(r"\b[a-z]{2}\b", low) or "," in text or len(text) > 3
    ):
        return "elsewhere"
    return "unknown"
