import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

from sqlalchemy import and_, or_

from models import CampaignRecipient, CompanyContact, OutboundEmail, Suppression
from services.contact_selection import normalized_contact_email, select_email_contact
from services.safe_http import normalize_http_url


TEMPLATE_FIELDS = {
    "{company_name}": lambda company: company.official_name or "",
    "{municipality}": lambda company: company.municipality or "",
    "{ico}": lambda company: company.ico or "",
}
OPT_OUT_FOOTER = (
    "Ak si neželáte ďalšie správy, stačí odpovedať „neposielať“."
)
VALIDATION_NOTICE = (
    "Aktuálne overujeme záujem o pripravované riešenie; nejde ešte o hotový produkt."
)
LEADING_GREETING_RE = re.compile(
    r"^\s*dobr[ýy]\s+de[nň]\s*[,!:\-–—]*\s*",
    re.IGNORECASE,
)
QUESTION_SENTENCE_RE = re.compile(
    r"(^|(?<=[.!])\s+|\n+)[^.!?\n]*\?",
    re.MULTILINE,
)
OUTREACH_OPENING_RE = re.compile(
    r"\b(?:aktuálne|momentálne|overujeme|ponúkame|pomáhame|pripravujeme|vyvíjame)\b",
    re.IGNORECASE,
)
REPLY_REQUEST_SENTENCE_RE = re.compile(
    r"(^|(?<=[.!])\s+|\n+)(?:dajte nám vedieť|napíšte nám|ozvite sa|"
    r"prosíme|radi by sme (?:overili|zistili|vedeli))[^.!?\n]*[.!?]",
    re.IGNORECASE | re.MULTILINE,
)
ADAM_SIGNATURE_RE = re.compile(
    r"\s*S pozdravom\s*,?\s*(?:\n\s*)?Adam\b",
    re.IGNORECASE,
)
QUOTED_REPLY_MARKER_RE = re.compile(
    r"^\s*(?:"
    r">|"
    r"[-_]{2,}\s*(?:original message|p[oô]vodn[aá] spr[aá]va)|"
    r"on\s+.+\s+wrote:|"
    r"(?:d[nň]a|v)\s+.+\s+nap[ií]sal(?:a|\(a\))?:|"
    r"dne\s+.+\s+napsal(?:a)?:|"
    r"(?:from|od|sent|odoslan[eé]|to|komu|subject|predmet):\s+"
    r")",
    re.IGNORECASE,
)
EXPLICIT_OPT_OUT_REPLY_RE = re.compile(
    r"(?:(?:dobry den|ahoj)\s+)?"
    r"(?:nemam zaujem\s+)?"
    r"(?:prosim\s+)?"
    r"(?:"
    r"(?:neposielat|neposielajte)"
    r"(?:\s+(?:mi|nam))?"
    r"(?:\s+(?:dalsie\s+)?(?:spravy|e\s*maily|emaily|maily))?"
    r"|(?:odhlasit|odhlaste)(?:\s+(?:ma|nas))?"
    r"|(?:uz\s+)?(?:ma|nas)\s+(?:prosim\s+)?nekontaktujte"
    r"|(?:vymazte|odstrante)\s+"
    r"(?:(?:moj|muj|moju)\s+(?:e\s*mail|email|emailovu\s+adresu)|(?:ma|me|nas))"
    r"(?:\s+(?:z|zo|ze)\s+(?:(?:vasej|vasi)\s+)?"
    r"(?:databazy|zoznamu\s+kontaktov|seznamu\s+kontaktu))?"
    r"|(?:chcem\s+sa\s+)?odhlasit"
    r")"
    r"(?:\s+(?:dakujem|vdaka))?"
)


def normalize_email(value):
    return (value or "").strip().casefold()


def normalize_suppression_value(scope, value):
    normalized = (value or "").strip().casefold()
    if scope == "domain":
        normalized = normalized.removeprefix("@")
    elif scope == "company":
        normalized = "".join(character for character in normalized if character.isalnum())
    return normalized


def company_suppression_value(company):
    return normalize_suppression_value("company", company.ico or str(company.id))


def email_domain(email):
    normalized = normalize_email(email)
    if normalized.count("@") != 1:
        return ""
    return normalized.rsplit("@", 1)[1]


def is_suppressed(company, email):
    normalized_email = normalize_email(email)
    domain = email_domain(normalized_email)
    company_value = company_suppression_value(company)
    clauses = [
        and_(Suppression.scope == "email", Suppression.value == normalized_email),
        and_(Suppression.scope == "company", Suppression.value == company_value),
    ]
    if domain:
        clauses.append(
            and_(Suppression.scope == "domain", Suppression.value == domain)
        )
    return Suppression.query.filter(or_(*clauses)).first()


def best_email_contact(company, *, require_verified=False):
    return select_email_contact(
        company.contacts,
        require_verified=require_verified,
    )


def render_campaign_template(template, company, *, email_source_url=None):
    rendered = template or ""
    for placeholder, getter in TEMPLATE_FIELDS.items():
        rendered = rendered.replace(placeholder, getter(company))
    if email_source_url is not None:
        rendered = rendered.replace("{email_source_url}", email_source_url)
    return rendered.strip()


def _public_disclosure_url(value):
    """Validate a public link syntactically; never fetch recipient data here."""
    normalized = normalize_http_url(value)
    if not normalized or any(character.isspace() or character in "{}<>" for character in normalized):
        return None
    hostname = urlsplit(normalized).hostname.casefold()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None:
        return normalized if address.is_global else None
    if (
        "." not in hostname
        or hostname.rsplit(".", 1)[1].isdigit()
        or hostname.endswith((".local", ".localhost", ".internal", ".invalid", ".test", ".localdomain"))
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in hostname.split("."))
    ):
        return None
    return normalized


def ensure_campaign_privacy_disclosure(body, campaign, contact):
    """Append the approved first layer only for campaigns that explicitly opt in.

    Source links come from the selected contact, never from AI-generated text.
    The caller must skip delivery/preparation when validation raises ValueError.
    """
    profile = campaign.targeting_profile or {}
    if "privacy_notice_url" not in profile:
        return body
    notice_url = _public_disclosure_url(profile.get("privacy_notice_url"))
    source_url = _public_disclosure_url(getattr(contact, "source_url", None))
    if not notice_url or not source_url:
        raise ValueError("Kampaň vyžaduje verejnú informačnú stránku a presný verejný zdroj e-mailu.")
    text = (body or "").replace("{email_source_url}", source_url).strip()
    if re.search(r"\{[^{}\r\n]+\}|<\s*DOPLNI", text, re.IGNORECASE):
        raise ValueError("Správa obsahuje nedoplnený placeholder.")
    disclosure = (
        "Vašu pracovnú e-mailovú adresu som získal z verejne dostupného firemného profilu prevádzky. "
        f"Zdroj kontaktu: {source_url}. "
        "Používam ju na toto obmedzené B2B oslovenie na základe oprávneného záujmu. "
        f"Podrobnosti: {notice_url}. "
        "Proti priamemu marketingu môžete kedykoľvek bezplatne namietať odpoveďou „neposielať“; "
        "po námietke vám už marketingové správy neposielam."
    )
    if disclosure in text:
        return text
    had_footer = text.endswith(OPT_OUT_FOOTER)
    if had_footer:
        text = text[:-len(OPT_OUT_FOOTER)].rstrip()
    text = f"{text}\n\n{disclosure}".strip()
    return ensure_opt_out_footer(text) if had_footer else text


def clean_automated_outreach_body(body, company_name=None):
    """Remove AI salutation/personalized greeting and reply-seeking questions."""
    cleaned = LEADING_GREETING_RE.sub("", (body or "").strip(), count=1)
    if company_name:
        company_prefix = re.compile(
            rf"^\s*{re.escape(str(company_name).strip())}\s*[,!:\-–—]*\s*",
            re.IGNORECASE,
        )
        cleaned = company_prefix.sub("", cleaned, count=1)
    opening = OUTREACH_OPENING_RE.search(cleaned[:200])
    if opening and opening.start() > 0:
        cleaned = cleaned[opening.start():]
    cleaned = QUESTION_SENTENCE_RE.sub(lambda match: match.group(1), cleaned)
    cleaned = REPLY_REQUEST_SENTENCE_RE.sub(lambda match: match.group(1), cleaned)
    cleaned = ADAM_SIGNATURE_RE.sub("\n\nS pozdravom\nAdam", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def ensure_opt_out_footer(body):
    normalized_body = (body or "").strip()
    if OPT_OUT_FOOTER.casefold() in normalized_body.casefold():
        return normalized_body
    return f"{normalized_body}\n\n{OPT_OUT_FOOTER}".strip()


def ensure_validation_disclosure(body):
    normalized_body = (body or "").strip()
    lowered = normalized_body.casefold()
    if any(word in lowered for word in ("priprav", "overuj", "valida", "testuj")):
        return normalized_body
    return f"{VALIDATION_NOTICE}\n\n{normalized_body}".strip()


def extract_new_reply_segment(body):
    """Return only the sender's text before common quoted-message markers."""
    reply_lines = []
    for line in (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if QUOTED_REPLY_MARKER_RE.match(line):
            break
        reply_lines.append(line)
    return "\n".join(reply_lines).strip()


def _normalize_reply_text(value):
    decomposed = unicodedata.normalize("NFKD", value or "")
    without_accents = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    words_only = re.sub(r"[^a-z0-9]+", " ", without_accents.casefold())
    return " ".join(words_only.split())


def is_explicit_opt_out_reply(body):
    """Recognize short explicit opt-outs without scanning quoted campaign text."""
    normalized = _normalize_reply_text(extract_new_reply_segment(body))
    if not normalized or len(normalized) > 200:
        return False
    return bool(EXPLICIT_OPT_OUT_REPLY_RE.fullmatch(normalized))


def valid_recipient_contact(contact, company):
    return (
        isinstance(contact, CompanyContact)
        and contact.company_id == company.id
        and contact.contact_type.casefold() == "email"
        and normalized_contact_email(contact) is not None
    )


def campaign_recipient_for_message_ids(message_ids, lead=None, sender_email=None):
    normalized_ids = [message_id for message_id in (message_ids or []) if message_id]
    query = OutboundEmail.query.filter(
        OutboundEmail.campaign_recipient_id.isnot(None),
    )
    if normalized_ids:
        query = query.filter(OutboundEmail.message_id.in_(normalized_ids))
    elif lead is not None:
        query = query.filter(OutboundEmail.lead_id == lead.id)
        if sender_email:
            query = query.filter(
                OutboundEmail.recipient == normalize_email(sender_email)
            )
    else:
        return None
    outbound = query.order_by(OutboundEmail.sent_at.desc()).first()
    return outbound.campaign_recipient if outbound else None


def mark_campaign_recipient_replied(
    recipient,
    received_at,
    reply_body=None,
    sender_email=None,
):
    """Update campaign outcome and return an email suppression for explicit opt-out."""
    opted_out = is_explicit_opt_out_reply(reply_body)
    suppression = None

    if opted_out:
        suppression_email = normalize_email(
            recipient.recipient_email
            if isinstance(recipient, CampaignRecipient)
            else sender_email
        )
        if suppression_email.count("@") == 1:
            suppression = Suppression.query.filter_by(
                scope="email",
                value=suppression_email,
            ).first()
            if suppression is None:
                suppression = Suppression(
                    scope="email",
                    value=suppression_email,
                    reason="Príjemca požiadal e-mailovou odpoveďou o ukončenie kontaktu.",
                )

    if isinstance(recipient, CampaignRecipient):
        if opted_out:
            recipient.status = "opted_out"
        elif recipient.status not in {"interested", "not_interested", "opted_out"}:
            recipient.status = "replied"
        recipient.replied_at = received_at

    return suppression
