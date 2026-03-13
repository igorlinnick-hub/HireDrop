import imaplib
import email
from email.header import decode_header
from config import EMAIL_ADDRESS, EMAIL_PASSWORD, EMAIL_IMAP_SERVER

KEYWORDS = ["interview", "application received", "next step", "thank you for applying"]


def decode_header_value(value):
    decoded_parts = decode_header(value)
    result = ""
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            result += part.decode(charset or "utf-8", errors="replace")
        else:
            result += part
    return result


def check_email_responses():
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

        for msg_id in message_ids[0].split():
            _, msg_data = mail.fetch(msg_id, "(BODY.PEEK[])")
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject = decode_header_value(msg.get("Subject", ""))
            sender = decode_header_value(msg.get("From", ""))
            date = msg.get("Date", "")

            subject_lower = subject.lower()
            if any(kw in subject_lower for kw in KEYWORDS):
                results.append({
                    "subject": subject,
                    "sender": sender,
                    "date": date,
                })

        mail.logout()
    except imaplib.IMAP4.error as e:
        print(f"[email] IMAP error: {e}")
    except Exception as e:
        print(f"[email] Failed to check emails: {e}")

    return results
