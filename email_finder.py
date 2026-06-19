import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse


EMAIL_REGEX = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"


BAD_EMAIL_PARTS = [
    "example",
    "test@",
    "your@email",
    "email@domain",
    "sentry",
    "wixpress",
    "wordpress",
    "schema.org",
]


CONTACT_PATHS = [
    "",
    "/kontakt",
    "/kontakty",
    "/contact",
    "/o-nas",
    "/o-nás",
]


def normalize_url(url):
    if not url:
        return None

    url = url.strip()

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    return url


def is_valid_email(email):
    email = email.lower().strip()

    if any(bad in email for bad in BAD_EMAIL_PARTS):
        return False

    if email.endswith((".png", ".jpg", ".jpeg", ".webp", ".svg")):
        return False

    return True


def extract_emails_from_text(text):
    emails = re.findall(EMAIL_REGEX, text or "")
    clean_emails = []

    for email in emails:
        email = email.strip().lower()

        if is_valid_email(email) and email not in clean_emails:
            clean_emails.append(email)

    return clean_emails


def score_email(email):
    """
    Nižšie skóre = lepší email.
    """
    email = email.lower()

    priority = [
        "info@",
        "kontakt@",
        "office@",
        "obchod@",
        "predaj@",
        "mail@",
    ]

    for index, prefix in enumerate(priority):
        if email.startswith(prefix):
            return index

    return 99


def find_email_on_website(website):
    website = normalize_url(website)

    if not website:
        return None, []

    found_emails = []

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    for path in CONTACT_PATHS:
        try:
            url = urljoin(website, path)

            response = requests.get(
                url,
                headers=headers,
                timeout=8,
                allow_redirects=True
            )

            if response.status_code >= 400:
                continue

            soup = BeautifulSoup(response.text, "html.parser")

            # odstráni bordel
            for tag in soup(["script", "style"]):
                tag.decompose()

            text = soup.get_text(" ")

            # emaily z textu
            emails = extract_emails_from_text(text)

            # emaily z mailto odkazov
            for link in soup.find_all("a", href=True):
                href = link["href"]

                if href.startswith("mailto:"):
                    email = href.replace("mailto:", "").split("?")[0]
                    emails.append(email)

            for email in emails:
                email = email.lower().strip()

                if is_valid_email(email) and email not in found_emails:
                    found_emails.append(email)

        except requests.RequestException:
            continue

    if not found_emails:
        return None, []

    found_emails = sorted(found_emails, key=score_email)

    return found_emails[0], found_emails