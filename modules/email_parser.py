import email
import imaplib
import re
from email.header import decode_header

from config import EMAIL_ADDRESS, EMAIL_IMAP_SERVER, EMAIL_PASSWORD

INTERVIEW_KEYWORDS = ["interview", "next step", "schedule a call", "speaking with you"]
REJECTION_KEYWORDS = ["unfortunately", "not moving forward", "decided to pursue", "not selected", "other candidates"]
RECEIVED_KEYWORDS = ["application received", "thank you for applying", "we received your application"]

_COMPANY_PATTERNS = [
    re.compile(r"from ([A-Z][A-Za-z0-9& ]+?) regarding", re.I),
    re.compile(r"application (?:to|at|with) ([A-Z][A-Za-z0-9& ,\.]+?)[\s,!]", re.I),
    re.compile(r"team at ([A-Z][A-Za-z0-9& ]+)", re.I),
    re.compile(r"^([A-Z][A-Za-z0-9& ]+?) (?:- |–)", re.I),
]


def _classify(subject: str) -> str:
    s = subject.lower()
    if any(kw in s for kw in INTERVIEW_KEYWORDS):
        return "interview_invite"
    if any(kw in s for kw in REJECTION_KEYWORDS):
        return "rejected"
    if any(kw in s for kw in RECEIVED_KEYWORDS):
        return "received"
    return "other"


def _extract_company(subject: str, sender: str) -> str:
    for pat in _COMPANY_PATTERNS:
        m = pat.search(subject)
        if m:
            return m.group(1).strip(" ,.")
    domain_match = re.search(r"@([^.>]+)\.", sender)
    if domain_match:
        raw = domain_match.group(1)
        if raw.lower() not in ("gmail", "yahoo", "outlook", "hotmail", "indeed", "lever", "greenhouse"):
            return raw.capitalize()
    return ""


def decode_header_value(value):
    decoded_parts = decode_header(value)
    result = ""
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            if not charset or charset.lower() in ("unknown-8bit", "unknown"):
                charset = "utf-8"
            try:
                result += part.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                result += part.decode("utf-8", errors="replace")
        else:
            result += part
    return result


def check_email_responses() -> list:
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        print("[email] Email credentials not configured")
        return []

    results = []

    try:
        mail = imaplib.IMAP4_SSL(EMAIL_IMAP_SERVER)
        mail.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        mail.select("inbox")

        _, message_ids = mail.search(None, "UNSEEN")
        if not message_ids[0]:
            mail.logout()
            return []

        all_keywords = INTERVIEW_KEYWORDS + REJECTION_KEYWORDS + RECEIVED_KEYWORDS

        for msg_id in message_ids[0].split():
            _, msg_data = mail.fetch(msg_id, "(BODY.PEEK[])")
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject = decode_header_value(msg.get("Subject", ""))
            sender = decode_header_value(msg.get("From", ""))
            date = msg.get("Date", "")

            subject_lower = subject.lower()
            if any(kw in subject_lower for kw in all_keywords):
                results.append(
                    {
                        "subject": subject,
                        "sender": sender,
                        "date": date,
                        "email_status": _classify(subject),
                        "company": _extract_company(subject, sender),
                    }
                )

        mail.logout()
    except imaplib.IMAP4.error as e:
        print(f"[email] IMAP error: {e}")
    except Exception as e:
        print(f"[email] Failed to check emails: {e}")

    return results
