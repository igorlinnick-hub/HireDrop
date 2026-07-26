"""Disposable/throwaway email domains — free-taste abuse guard (FREE_TASTE_PLAN.md §3).

Each free account costs us ~$0.80 in AI spend, so farming accounts on temp-mail
services is the one real abuse vector. Signup itself lives in Supabase Auth (no
backend endpoint), so the gate sits on the first thing a fresh account must do
to spend our money: POST /campaign/start.

Denylist, not a 3rd-party API: small, zero-latency, no external dependency.
Covers the major disposable providers + their alias domains. Extend the set as
new farms show up in the signup logs.
"""

# Lowercase, exact-match on the part after "@". Subdomains match too
# (e.g. anything@mail.mailinator.com).
DISPOSABLE_DOMAINS = frozenset({
    # Mailinator family
    "mailinator.com", "mailinator.net", "mailinator.org", "mailinater.com",
    "reconmail.com", "safetymail.info", "sogetthis.com", "spamherelots.com",
    # temp-mail.org family
    "temp-mail.org", "temp-mail.io", "tempmail.com", "tempmail.net",
    "tempmail.dev", "tempmailo.com", "tempail.com", "temporarymail.com",
    # Guerrilla Mail family
    "guerrillamail.com", "guerrillamail.net", "guerrillamail.org",
    "guerrillamail.biz", "guerrillamail.de", "guerrillamail.info",
    "grr.la", "sharklasers.com", "spam4.me", "pokemail.net",
    # 10 Minute Mail family
    "10minutemail.com", "10minutemail.net", "10minemail.com",
    "20minutemail.com", "10minutesmail.com",
    # YOPmail family
    "yopmail.com", "yopmail.fr", "yopmail.net", "cool.fr.nf",
    "jetable.fr.nf", "courriel.fr.nf", "moncourrier.fr.nf",
    # Other high-volume providers
    "getnada.com", "nada.email", "inboxkitten.com", "maildrop.cc",
    "dispostable.com", "mintemail.com", "throwawaymail.com", "trashmail.com",
    "trashmail.de", "mailnesia.com", "mytemp.email", "burnermail.io",
    "mohmal.com", "emailondeck.com", "fakeinbox.com", "spamgourmet.com",
    "mailcatch.com", "moakt.com", "tmpmail.org", "tmpmail.net",
    "disposablemail.com", "mail-temp.com", "email-fake.com", "fakemail.net",
    "crazymailing.com", "tempinbox.com", "mailsac.com", "inboxbear.com",
    "33mail.com", "spambox.us", "mailnull.com", "incognitomail.org",
    "anonbox.net", "deadaddress.com", "emailsensei.com", "spamspot.com",
    "harakirimail.com", "meltmail.com", "mailexpire.com", "trbvm.com",
    "armyspy.com", "cuvox.de", "dayrep.com", "einrot.com", "fleckens.hu",
    "gustr.com", "jourrapide.com", "rhyta.com", "superrito.com", "teleworm.us",
})


def is_disposable_email(email: str | None) -> bool:
    """True if the email's domain (or any parent domain) is a known throwaway."""
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    parts = domain.split(".")
    return any(".".join(parts[i:]) in DISPOSABLE_DOMAINS for i in range(len(parts) - 1))
