import os
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime


IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.gmail.com")
IMAP_USERNAME = os.environ.get("IMAP_USERNAME")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD")


def decode_mime_words(value):
    if not value:
        return ""

    decoded_parts = decode_header(value)
    result = ""

    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            result += part.decode(encoding or "utf-8", errors="ignore")
        else:
            result += part

    return result


def extract_text_from_email(msg):
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            if content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")

    return ""


def check_reply_from_sender(sender_email, after_datetime=None):
    """
    Skontroluje, či v inboxe existuje email od sender_email.
    Ak after_datetime existuje, berie iba emaily po tomto dátume.
    """

    if not IMAP_USERNAME or not IMAP_PASSWORD:
        raise ValueError("Chýba IMAP_USERNAME alebo IMAP_PASSWORD v .env súbore.")

    if not sender_email:
        raise ValueError("Lead nemá email adresu.")

    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(IMAP_USERNAME, IMAP_PASSWORD)
    mail.select("inbox")

    search_query = f'(FROM "{sender_email}")'

    status, data = mail.search(None, search_query)

    if status != "OK":
        mail.logout()
        return None

    email_ids = data[0].split()

    if not email_ids:
        mail.logout()
        return None

    # Najnovšie správy ako prvé
    email_ids = list(reversed(email_ids))

    for email_id in email_ids:
        status, msg_data = mail.fetch(email_id, "(RFC822)")

        if status != "OK":
            continue

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject = decode_mime_words(msg.get("Subject", ""))
        from_header = decode_mime_words(msg.get("From", ""))
        date_header = msg.get("Date", "")

        try:
            received_at = parsedate_to_datetime(date_header)
            if received_at and received_at.tzinfo:
                received_at = received_at.replace(tzinfo=None)
        except Exception:
            received_at = None

        if after_datetime and received_at:
            if received_at <= after_datetime:
                continue

        body = extract_text_from_email(msg)

        mail.logout()

        return {
            "from": from_header,
            "subject": subject,
            "received_at": received_at,
            "body": body.strip()[:2000],
        }

    mail.logout()
    return None