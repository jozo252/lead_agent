from sqlalchemy import and_, or_

from models import CampaignRecipient, CompanyContact, OutboundEmail, Suppression


TEMPLATE_FIELDS = {
    "{company_name}": lambda company: company.official_name or "",
    "{municipality}": lambda company: company.municipality or "",
    "{ico}": lambda company: company.ico or "",
}
OPT_OUT_FOOTER = (
    "Ak si neželáte ďalšie správy, stačí odpovedať „neposielať“."
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


def best_email_contact(company):
    contacts = [
        contact
        for contact in company.contacts
        if contact.contact_type.casefold() == "email" and normalize_email(contact.value)
    ]
    if not contacts:
        return None
    return max(
        contacts,
        key=lambda contact: (
            bool(contact.is_primary),
            bool(contact.is_verified),
            contact.confidence_score or 0,
        ),
    )


def render_campaign_template(template, company):
    rendered = template or ""
    for placeholder, getter in TEMPLATE_FIELDS.items():
        rendered = rendered.replace(placeholder, getter(company))
    return rendered.strip()


def ensure_opt_out_footer(body):
    normalized_body = (body or "").strip()
    if OPT_OUT_FOOTER.casefold() in normalized_body.casefold():
        return normalized_body
    return f"{normalized_body}\n\n{OPT_OUT_FOOTER}".strip()


def valid_recipient_contact(contact, company):
    return (
        isinstance(contact, CompanyContact)
        and contact.company_id == company.id
        and contact.contact_type.casefold() == "email"
        and bool(normalize_email(contact.value))
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


def mark_campaign_recipient_replied(recipient, received_at):
    if not isinstance(recipient, CampaignRecipient):
        return
    if recipient.status not in {"interested", "not_interested", "opted_out"}:
        recipient.status = "replied"
    recipient.replied_at = received_at
