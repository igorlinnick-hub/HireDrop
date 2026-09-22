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


def complete_keywords(existing, extra, limit: int) -> tuple[list[str], list[str]]:
    """Pad a thin keyword set with resume-derived roles, up to `limit`.

    The whole engine downstream — rotation, breadth walk, the daily cap — is only as good
    as the roles fed in. Measured 2026-09-22: a broad-mode run carried four hand-typed
    words, one of them ("health care") pulling only licensed clinical roles the résumé
    could never pass; the run opened 25 postings and applied to zero. The gates worked;
    the INPUT was the ceiling. This raises the ceiling from the one source that isn't a
    blind guess — the résumé (see modules.ai_role_suggest) — without asking the user to
    invent words at an empty field.

    Rules, matching the product decision (Igor, 2026-09-22):
      * User keywords come FIRST and are never dropped — their intent leads.
      * Fill the remaining slots with résumé roles NOT already present (case-insensitive),
        so we don't restate a word the user already typed.
      * Stop at `limit` — the apply mode's role budget (broad 7 / standard 5 / precise 3).
        "Don't invent too many different vacancies": the mode caps breadth.
      * If the user is already at/over the limit, change NOTHING (never trim their list).

    Returns (full_set, added) — `added` is exactly the résumé roles appended, in order, so
    the caller can name them in the activity feed instead of changing the search silently.
    """
    base = clean_keywords(existing)
    if not limit or len(base) >= limit:
        return base, []
    seen = {term.casefold() for term in base}
    full = list(base)
    added: list[str] = []
    for role in clean_keywords(extra):
        if len(full) >= limit:
            break
        key = role.casefold()
        if key in seen:
            continue
        seen.add(key)
        full.append(role)
        added.append(role)
    return full, added
