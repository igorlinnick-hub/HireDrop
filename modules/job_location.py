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
        elif bare not in ("us", "usa", "united states") and city is None:
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
        if _NON_US_RE.search(low) and not _US_HINT_RE.search(low):
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
