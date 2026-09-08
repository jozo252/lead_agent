from decimal import Decimal, InvalidOperation

from sqlalchemy import false, or_

from extensions import db
from models import CampaignRecipient, Company, CompanyContact, Lead
from services.postal_locations import nearby_postal_codes
from services.website_presence import website_absence_filter


FILTER_NAMES = (
    "entity",
    "q",
    "sk_nace",
    "region",
    "contacts",
    "email",
    "website",
    "analyzed",
    "financials",
    "min_revenue",
    "max_revenue",
    "contacted",
    "near",
    "radius_km",
    "relevant",
    "abroad",
    "subcontractor_need",
)

ENTITY_COMPANY = "company"
ENTITY_SOLE_TRADER = "sole_trader"
ENTITY_ALL = "all"
VALID_ENTITY_FILTERS = {
    ENTITY_COMPANY,
    ENTITY_SOLE_TRADER,
    ENTITY_ALL,
}
SOLE_TRADER_LEGAL_FORM_PREFIX = "Podnikateľ-fyzická osoba"


def company_filters_from_source(source):
    filters = {name: source.get(name, "").strip() for name in FILTER_NAMES}
    if filters["entity"] not in VALID_ENTITY_FILTERS:
        filters["entity"] = ENTITY_COMPANY
    return filters


def sole_trader_condition():
    return Company.legal_form.ilike(f"{SOLE_TRADER_LEGAL_FORM_PREFIX}%")


def companies_for_entity(entity):
    query = Company.query
    if entity == ENTITY_SOLE_TRADER:
        return query.filter(sole_trader_condition())
    if entity == ENTITY_ALL:
        return query
    return query.filter(
        or_(
            Company.legal_form.is_(None),
            ~sole_trader_condition(),
        )
    )


def company_entity(company):
    legal_form = (company.legal_form or "").strip().casefold()
    if legal_form.startswith(SOLE_TRADER_LEGAL_FORM_PREFIX.casefold()):
        return ENTITY_SOLE_TRADER
    return ENTITY_COMPANY


def decimal_filter_value(value):
    try:
        return Decimal((value or "").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None


def radius_filter_value(value):
    try:
        radius = float((value or "").replace(",", "."))
    except ValueError:
        return None
    return radius if 1 <= radius <= 200 else None


def filtered_companies_query(filters):
    query = companies_for_entity(filters.get("entity"))

    if filters["q"]:
        pattern = f"%{filters['q']}%"
        query = query.filter(
            or_(
                Company.ico.ilike(pattern),
                Company.official_name.ilike(pattern),
                Company.municipality.ilike(pattern),
            )
        )

    if filters["sk_nace"]:
        pattern = f"%{filters['sk_nace']}%"
        query = query.filter(
            or_(
                Company.sk_nace_code.ilike(pattern),
                Company.sk_nace_name.ilike(pattern),
            )
        )

    if filters["region"]:
        query = query.filter(Company.municipality.ilike(f"%{filters['region']}%"))

    if filters["contacts"] == "any":
        query = query.filter(Company.contacts.any())
    elif filters["contacts"] == "verified":
        query = query.filter(
            Company.contacts.any(CompanyContact.is_verified.is_(True))
        )
    elif filters["contacts"] == "candidate":
        query = query.filter(
            Company.contacts.any(CompanyContact.is_verified.is_(False))
        )

    email_contact = CompanyContact.contact_type.ilike("email")
    if filters["email"] == "any":
        query = query.filter(Company.contacts.any(email_contact))
    elif filters["email"] == "verified":
        query = query.filter(
            Company.contacts.any(
                email_contact & CompanyContact.is_verified.is_(True)
            )
        )
    elif filters["email"] == "candidate":
        query = query.filter(
            Company.contacts.any(
                email_contact & CompanyContact.is_verified.is_(False)
            )
        )
    elif filters["email"] == "none":
        query = query.filter(~Company.contacts.any(email_contact))

    if filters["website"] == "yes":
        query = query.filter(
            Company.contacts.any(CompanyContact.contact_type == "website")
        )
    elif filters["website"] == "no":
        query = query.filter(
            ~Company.contacts.any(CompanyContact.contact_type == "website")
        )
    elif filters["website"] == "checked_not_found":
        query = query.filter(website_absence_filter())

    if filters["analyzed"] == "yes":
        query = query.filter(Company.website_analyzed_at.isnot(None))
    elif filters["analyzed"] == "no":
        query = query.filter(Company.website_analyzed_at.is_(None))

    if filters["financials"] == "yes":
        query = query.filter(Company.financials_status == "success")
    elif filters["financials"] == "no":
        query = query.filter(Company.financials_checked_at.is_(None))

    minimum_revenue = decimal_filter_value(filters["min_revenue"])
    maximum_revenue = decimal_filter_value(filters["max_revenue"])
    if minimum_revenue is not None:
        query = query.filter(Company.annual_revenue >= minimum_revenue)
    if maximum_revenue is not None:
        query = query.filter(Company.annual_revenue <= maximum_revenue)

    campaign_contact_exists = db.session.query(CampaignRecipient.id).filter(
        CampaignRecipient.company_id == Company.id,
        CampaignRecipient.sent_at.isnot(None),
    ).exists()
    legacy_contact_exists = db.session.query(Lead.id).filter(
        Lead.company_id == Company.id,
        Lead.last_contacted_at.isnot(None),
    ).exists()
    if filters["contacted"] == "yes":
        query = query.filter(or_(campaign_contact_exists, legacy_contact_exists))
    elif filters["contacted"] == "no":
        query = query.filter(~or_(campaign_contact_exists, legacy_contact_exists))

    if filters["near"]:
        radius = radius_filter_value(filters["radius_km"] or "20")
        if radius is None:
            query = query.filter(false())
        else:
            postal_codes = nearby_postal_codes(filters["near"], radius)
            query = query.filter(
                Company.postal_code.in_(postal_codes) if postal_codes else false()
            )

    if filters["relevant"] == "yes":
        query = query.filter(Company.outreach_relevant.is_(True))

    if filters["abroad"] == "yes":
        query = query.filter(Company.works_abroad.is_(True))

    if filters["subcontractor_need"] in {"low", "medium", "high"}:
        query = query.filter(
            Company.subcontractor_need == filters["subcontractor_need"]
        )

    return query
