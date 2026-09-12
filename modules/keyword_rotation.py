"""Round-robin over the roles a user searches for.

The walk searches ONE phrase at a time (content.js `currentSearchPhrase`), gives it
`max(4, 24/n)` pages, then rotates to the next — strictly in list order, always starting
at index 0. Every cap in the system counts APPLICATIONS, not searches: the daily tier
budget (30 in auto) is spent by whichever roles come first, so with six or seven roles
the tail of the list never gets a turn. This is the same failure the ATS watchlist hit
(#154): the order of a list silently became a priority no one chose, and the new entries
returned 0 of 160.

So the server hands out a different STARTING role each run and remembers where it stopped.
The set is unchanged — only which role leads. Over `len(keywords)` runs every role leads
exactly once.

The cursor lives in `campaign_states.filters` (the previous run's row), so this needs no
migration and no new table: the value that survives a run is already persisted there.
"""


def clean_keywords(keywords) -> list[str]:
    """Non-empty, trimmed, de-duplicated (case-insensitively), original order kept."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in keywords or []:
        term = str(raw).strip()
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


def rotate(keywords, cursor: int = 0) -> tuple[list[str], int]:
    """Return (this run's order, the cursor to store for the next run).

    The cursor is taken modulo the CURRENT list length, so editing the roles between runs
    can never point past the end or silently drop a role — it just lands somewhere valid.
    The next cursor advances by one: a role that led today is last to lead again, and the
    step stays explainable ("everyone leads once per <n> runs") instead of tracking how
    much budget each role happened to consume.
    """
    terms = clean_keywords(keywords)
    if not terms:
        return [], 0
    try:
        start = int(cursor)
    except (TypeError, ValueError):
        start = 0
    start %= len(terms)
    if start < 0:
        start += len(terms)
    return terms[start:] + terms[:start], (start + 1) % len(terms)
