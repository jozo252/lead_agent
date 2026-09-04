from __future__ import annotations

from email_validator import EmailNotValidError, validate_email

from extensions import db
from models import Lead, LeadActivity, Opportunity, utcnow
from services.campaigns import ensure_opt_out_footer
from services.opportunity_scout import normalize_source_url


BUSINESS_LINE_WORK_TYPES = {
    "construction": "Stavebné práce",
    "electrical": "Elektro",
    "software": "Iné",
    "general": "Iné",
}


def _clean_required(value, label, maximum):
    cleaned = " ".join(str(value or "").split())
    if not cleaned:
        raise ValueError(f"{label} je povinný údaj.")
    if len(cleaned) > maximum:
        raise ValueError(f"{label} môže mať najviac {maximum} znakov.")
    return cleaned


def _clean_optional(value, maximum):
    cleaned = " ".join(str(value or "").split())
    if len(cleaned) > maximum:
        raise ValueError(f"Hodnota môže mať najviac {maximum} znakov.")
    return cleaned or None


def _normalize_email(value):
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    try:
        normalized = validate_email(
            cleaned,
            check_deliverability=False,
        ).normalized.casefold()
    except EmailNotValidError as exc:
        raise ValueError("Zadaj platný overený e-mailový kontakt.") from exc
    if len(normalized) > 200:
        raise ValueError("E-mailový kontakt môže mať najviac 200 znakov.")
    return normalized


def _normalize_web_url(value, label, *, required=False, maximum=1500):
    cleaned = str(value or "").strip()
    if not cleaned and not required:
        return None
    normalized = normalize_source_url(cleaned)
    if normalized is None:
        raise ValueError(f"{label} musí byť platná HTTP alebo HTTPS adresa.")
    if len(normalized) > maximum:
        raise ValueError(f"{label} môže mať najviac {maximum} znakov.")
    return normalized


def _render_campaign_text(template, *, company_name, city):
    values = {
        "{company_name}": company_name,
        "{municipality}": city or "",
        "{ico}": "",
    }
    rendered = template or ""
    for placeholder, value in values.items():
        rendered = rendered.replace(placeholder, value)
    return rendered.strip()


def _lead_score(fit_score):
    try:
        score = int(fit_score or 0)
    except (TypeError, ValueError):
        score = 0
    return max(1, min(5, (score + 19) // 20))


def convert_verified_opportunity(
    opportunity,
    *,
    verification_confirmed=False,
    company_name,
    verification_note,
    contact_source_url,
    email=None,
    phone=None,
    website=None,
    city=None,
    now=None,
):
    """Create one reviewable CRM lead from a manually verified opportunity."""
    if not isinstance(opportunity, Opportunity):
        raise ValueError("Príležitosť neexistuje.")
    if opportunity.lead is not None:
        return opportunity.lead, False
    if not verification_confirmed:
        raise ValueError("Najprv potvrď manuálne overenie aktuálnosti a kontaktu.")

    company_name = _clean_required(company_name, "Názov firmy alebo zákazníka", 200)
    verification_note = _clean_required(
        verification_note,
        "Poznámka k overeniu",
        2000,
    )
    contact_source_url = _normalize_web_url(
        contact_source_url,
        "Zdroj kontaktu",
        required=True,
    )
    email = _normalize_email(email)
    phone = _clean_optional(phone, 100)
    if not email and not phone:
        raise ValueError("Doplň aspoň jeden manuálne overený e-mail alebo telefón.")
    website = _normalize_web_url(website, "Web firmy", maximum=300)
    city = _clean_optional(city, 100)

    subject = _render_campaign_text(
        opportunity.campaign.subject_template,
        company_name=company_name,
        city=city,
    )
    body = _render_campaign_text(
        opportunity.campaign.body_template,
        company_name=company_name,
        city=city,
    )
    if not subject or not body:
        raise ValueError("Kampaň nemá použiteľný predmet a text konceptu.")
    body = ensure_opt_out_footer(body)

    reason_lines = [
        "Manuálne overená príležitosť z lovca zákaziek.",
        f"Príležitosť: {opportunity.title}",
        f"Zdroj príležitosti: {opportunity.source_url}",
        f"Zdroj kontaktu: {contact_source_url}",
        f"Overenie: {verification_note}",
    ]
    if opportunity.fit_reason:
        reason_lines.insert(3, f"Dôvod zhody: {opportunity.fit_reason}")

    lead = Lead(
        company_name=company_name,
        website=website,
        email=email,
        phone=phone,
        city=city,
        country="Slovensko",
        source=f"Lovec zákaziek: {opportunity.source_name}"[:100],
        work_type=BUSINESS_LINE_WORK_TYPES.get(opportunity.business_line, "Iné"),
        lead_score=_lead_score(opportunity.fit_score),
        status="Osloviť",
        reason_to_contact="\n".join(reason_lines),
        suggested_subject=subject[:255],
        suggested_message=body,
    )
    db.session.add(lead)
    db.session.flush()

    timestamp = now or utcnow()
    opportunity.lead = lead
    opportunity.status = "converted"
    opportunity.verified_at = timestamp
    opportunity.verification_note = verification_note
    opportunity.contact_source_url = contact_source_url
    opportunity.converted_at = timestamp
    db.session.add(
        LeadActivity(
            lead=lead,
            activity_type="Poznámka",
            note=(
                "Lead vznikol z manuálne overenej príležitosti. "
                "Koncept oslovenia bol uložený na kontrolu; nič nebolo odoslané."
            ),
        )
    )
    return lead, True
