from extensions import db
from models import Lead
from services.campaigns import best_email_contact


def get_or_create_company_lead(company, email, work_type=None):
    """Keep the legacy CRM lead in sync without storing campaign history on it."""
    lead = Lead.query.filter_by(company_id=company.id).one_or_none()
    phone_contacts = sorted(
        [contact for contact in company.contacts if contact.contact_type == "phone"],
        key=lambda contact: (
            bool(contact.is_primary),
            bool(contact.is_verified),
            contact.confidence_score or 0,
        ),
        reverse=True,
    )
    website_contacts = sorted(
        [contact for contact in company.contacts if contact.contact_type == "website"],
        key=lambda contact: (
            bool(contact.is_primary),
            bool(contact.is_verified),
            contact.confidence_score or 0,
        ),
        reverse=True,
    )

    if lead is None:
        lead = Lead(
            company_id=company.id,
            company_name=company.official_name or company.ico or "Neznáma firma",
            source="RPO",
            country=company.country or "Slovensko",
        )
        db.session.add(lead)

    fallback_email = best_email_contact(company)
    lead.email = email or (fallback_email.value if fallback_email else lead.email)
    lead.phone = phone_contacts[0].value if phone_contacts else lead.phone
    lead.website = website_contacts[0].value if website_contacts else lead.website
    lead.address = company.street or lead.address
    lead.city = company.municipality or lead.city
    lead.company_segment = company.company_type or lead.company_segment
    lead.reason_to_contact = company.analysis_reason or lead.reason_to_contact
    if work_type:
        lead.work_type = work_type[:100]

    return lead
