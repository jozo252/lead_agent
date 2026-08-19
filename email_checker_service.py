import os
import imaplib
import email
import re
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime


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


def extract_thread_message_ids(msg):
    """Vráti Message-ID hodnoty z hlavičiek odpovede na e-mailové vlákno."""
    values = [
        msg.get("In-Reply-To", ""),
        msg.get("References", ""),
    ]
    return {
        message_id
        for value in values
        for message_id in re.findall(r"<[^<>\s]+>", value)
    }


def parse_inbox_message(msg):
    """Prevedie IMAP správu na bezpečný slovník pre uloženie do CRM."""
    from_header = decode_mime_words(msg.get("From", ""))
    from_name, from_email = parseaddr(from_header)
    received_at = None

    try:
        received_at = parsedate_to_datetime(msg.get("Date", ""))
        if received_at and received_at.tzinfo:
            received_at = received_at.replace(tzinfo=None)
    except (TypeError, ValueError):
        pass

    thread_message_ids = extract_thread_message_ids(msg)
    return {
        "from_email": (from_email or from_header).strip().lower(),
        "from_name": from_name or None,
        "subject": decode_mime_words(msg.get("Subject", "")),
        "body": extract_text_from_email(msg).strip()[:20000],
        "message_id": msg.get("Message-ID", "").strip() or None,
        "received_at": received_at,
        "thread_message_ids": sorted(thread_message_ids),
    }


def fetch_inbox_messages(max_messages=50):
    """Načíta posledné správy z inboxu bez označenia ako prečítané."""
    if not IMAP_USERNAME or not IMAP_PASSWORD:
        raise ValueError("Chýba IMAP_USERNAME alebo IMAP_PASSWORD v .env súbore.")

    connection = None
    try:
        connection = imaplib.IMAP4_SSL(IMAP_SERVER)
        connection.login(IMAP_USERNAME, IMAP_PASSWORD)
        status, _ = connection.select("inbox", readonly=True)

        if status != "OK":
            raise ValueError("IMAP sa nepripojil k inboxu.")

        status, data = connection.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []

        email_ids = list(reversed(data[0].split()))[:max_messages]
        messages = []

        for email_id in email_ids:
            status, message_data = connection.fetch(email_id, "(RFC822)")
            if status != "OK" or not message_data:
                continue

            raw_email = message_data[0][1]
            messages.append(parse_inbox_message(email.message_from_bytes(raw_email)))

        return messages
    finally:
        if connection is not None:
            try:
                connection.logout()
            except Exception:
                pass


def find_thread_email_ids(mail, message_ids):
    """Nájde IMAP správy, ktoré odkazujú na niektorý odoslaný Message-ID."""
    email_ids = set()

    for message_id in message_ids or []:
        for header in ("In-Reply-To", "References"):
            status, data = mail.search(
                None,
                "HEADER",
                header,
                message_id,
            )
            if status == "OK" and data and data[0]:
                email_ids.update(data[0].split())

    return sorted(email_ids, key=int, reverse=True)


def check_reply_from_sender(
    sender_email,
    after_datetime=None,
    outbound_message_ids=None,
):
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

    email_ids = find_thread_email_ids(mail, outbound_message_ids)
    matched_by_thread = bool(email_ids)

    if not email_ids:
        search_query = f'(FROM "{sender_email}")'
        status, data = mail.search(None, search_query)

        if status != "OK":
            mail.logout()
            return None

        email_ids = list(reversed(data[0].split())) if data and data[0] else []

    if not email_ids:
        mail.logout()
        return None

    for email_id in email_ids:
        status, msg_data = mail.fetch(email_id, "(RFC822)")

        if status != "OK":
            continue

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        thread_message_ids = extract_thread_message_ids(msg)
        if matched_by_thread:
            if not thread_message_ids.intersection(outbound_message_ids or []):
                continue

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
            "message_id": msg.get("Message-ID", "").strip() or None,
            "matched_by_thread": matched_by_thread,
            "thread_message_ids": sorted(thread_message_ids),
        }

    mail.logout()
    return None
