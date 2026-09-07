from datetime import datetime, date, timedelta
from reply_generator import generate_reply_to_customer

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from models import (
    Campaign,
    CampaignRecipient,
    Company,
    CompanyContact,
    EmailReply,
    Lead,
    LeadActivity,
    OutboundEmail,
    QuoteRequest,
    Suppression,
    SenderProfile,
)
from extensions import db, mail
from ai_service import generate_lead_message, analyze_lead
from flask_mail import Message
from lead_finder_service import search_places_text, build_search_queries
from email_checker_service import check_reply_from_sender, fetch_inbox_messages
from email_finder import find_email_on_website
from services.company_web_enrichment import enrich_company_from_website
from services.rpo_sync import enrich_company_contacts
from services.ruz_financials import enrich_company_financials
from services.company_filtering import (
    company_filters_from_source,
    filtered_companies_query,
)
from services.crm import get_or_create_company_lead
from services.hubspot import HubSpotError, sync_lead_to_hubspot
from services.campaigns import (
    campaign_recipient_for_message_ids,
    mark_campaign_recipient_replied,
    is_suppressed,
)
from services.postal_locations import LocationLookupError
from services.quote_requests import create_quote_request, submit_price_and_send
from services.campaign_followups import sync_profile_inbox
from services.manual_replies import ManualReplySendError, send_manual_reply
from services.email_addresses import normalize_email_subject, normalize_valid_email
from services.sender_profiles import SenderProfileError, send_profile_message
from services.safe_http import normalize_http_url
from email.utils import parseaddr
from sqlalchemy import false, func, or_, and_
from urllib.parse import urlsplit


main_bp = Blueprint("main", __name__)


def _safe_referrer(default_url):
    """Return only a same-origin referrer, otherwise the explicit local URL."""
    referrer = request.referrer
    if not referrer:
        return default_url
    try:
        target = urlsplit(referrer)
        origin = urlsplit(request.host_url)
    except ValueError:
        return default_url
    if (
        target.scheme == origin.scheme
        and target.netloc.casefold() == origin.netloc.casefold()
    ):
        # Return a local absolute path. Collapsing leading slashes prevents a
        # same-origin referrer path such as //attacker.example from becoming a
        # scheme-relative redirect.
        path = "/" + (target.path or "/").lstrip("/")
        return path + (f"?{target.query}" if target.query else "")
    return default_url


def _manual_send_blocker(company, address, lead=None):
    """Legacy forms may not bypass a campaign account or an opt-out."""
    normalized = normalize_valid_email(address)
    if not normalized:
        return "Príjemca nemá platnú e-mailovú adresu."
    if company is not None and is_suppressed(company, normalized):
        return "Príjemca je na suppression zozname; správa nebola odoslaná."
    if Suppression.query.filter(or_(
        and_(Suppression.scope == "email", Suppression.value == normalized),
        and_(Suppression.scope == "domain", Suppression.value == normalized.rsplit("@", 1)[-1]),
    )).first():
        return "Príjemca je na suppression zozname; správa nebola odoslaná."
    if company is not None and CampaignRecipient.query.join(Campaign).filter(
        CampaignRecipient.company_id == company.id,
        Campaign.sender_profile_id.isnot(None),
    ).first():
        return "Firma patrí do kampane s vlastným profilom. Odošli správu cez túto kampaň, nie cez globálnu schránku."
    history = OutboundEmail.query.filter(OutboundEmail.sender_profile_id.isnot(None))
    if lead is not None:
        history = history.filter(OutboundEmail.lead_id == lead.id)
    elif company is not None:
        history = history.join(Lead).filter(Lead.company_id == company.id)
    else:
        return None
    if history.first():
        return "Kontakt má históriu vlastného profilu. Použi jeho kampaň alebo odpoveď v Inboxe; globálna schránka sa nepoužila."
    return None


WORK_TYPES = [
    "Elektro",
    "Stavebné práce",
    "Zváranie / kovovýroba",
    "Montáže",
    "Iné"
]


COMPANY_SEGMENTS = [
    "Kuchynské štúdio",
    "Rekonštrukčná firma",
    "Stavebná firma",
    "Správca bytov",
    "FVE / tepelné čerpadlá / klimatizácie",
    "Stolárstvo",
    "Interiérové štúdio",
    "Developer",
    "Priemyselná firma",
    "Iné"
]


LEAD_STATUSES = [
    "Nový",
    "Skontrolovať",
    "Osloviť",
    "Oslovený",
    "Odpovedal",
    "Telefonát",
    "Obhliadka",
    "Cenová ponuka",
    "Vyhraté",
    "Prehraté",
    "Nezaujímavé"
]

ACTIVITY_TYPES = [
    "Poznámka",
    "Email odoslaný",
    "Follow-up nastavený",
    "Telefonát",
    "Odpoveď",
    "Obhliadka",
    "Cenová ponuka",
    "Vyhraté",
    "Prehraté"
]

OUTREACH_INDUSTRIES = ["IT", "Elektro", "Stavby"]

COMPANIES_PER_PAGE = 100
TERMINAL_LEAD_STATUSES = {"Vyhraté", "Prehraté", "Nezaujímavé"}


def dashboard_metrics():
    """Return dashboard counts based only on persisted CRM records."""
    contacted_leads = (
        db.session.query(func.count(func.distinct(OutboundEmail.lead_id)))
        .scalar()
        or 0
    )
    responded_contacted_leads = (
        db.session.query(func.count(func.distinct(EmailReply.lead_id)))
        .join(OutboundEmail, OutboundEmail.lead_id == EmailReply.lead_id)
        .filter(EmailReply.lead_id.isnot(None))
        .scalar()
        or 0
    )
    reply_leads = (
        db.session.query(func.count(func.distinct(EmailReply.lead_id)))
        .filter(EmailReply.lead_id.isnot(None))
        .scalar()
        or 0
    )

    return {
        "companies": Company.query.count(),
        "companies_with_email": (
            db.session.query(func.count(func.distinct(CompanyContact.company_id)))
            .filter(func.lower(CompanyContact.contact_type) == "email")
            .scalar()
            or 0
        ),
        "contacts": CompanyContact.query.count(),
        "leads": Lead.query.count(),
        "contacted_leads": contacted_leads,
        "replies": reply_leads,
        "open_follow_ups": Lead.query.filter(
            Lead.next_follow_up_at.isnot(None),
            db.func.date(Lead.next_follow_up_at) <= date.today(),
            or_(
                Lead.status.is_(None),
                ~Lead.status.in_(TERMINAL_LEAD_STATUSES),
            ),
        ).count(),
        "reply_rate": (
            round((responded_contacted_leads / contacted_leads) * 100, 1)
            if contacted_leads
            else 0
        ),
    }


def dashboard_work_queues(limit=8):
    """Return the most important CRM actions for the current day."""
    today = date.today()
    reply_cutoff = datetime.combine(
        today - timedelta(days=5),
        datetime.min.time(),
    )
    reply_after_contact = (
        db.session.query(EmailReply.id)
        .filter(
            EmailReply.lead_id == Lead.id,
            EmailReply.received_at >= Lead.last_contacted_at,
        )
        .exists()
    )
    active_lead = or_(
        Lead.status.is_(None),
        ~Lead.status.in_(TERMINAL_LEAD_STATUSES),
    )

    return {
        "due_follow_ups": Lead.query.filter(
            Lead.next_follow_up_at.isnot(None),
            db.func.date(Lead.next_follow_up_at) <= today,
            active_lead,
        ).order_by(
            Lead.next_follow_up_at,
            Lead.lead_score.desc(),
        ).limit(limit).all(),
        "waiting_for_reply": Lead.query.filter(
            Lead.status == "Oslovený",
            Lead.last_contacted_at.isnot(None),
            Lead.last_contacted_at <= reply_cutoff,
            ~reply_after_contact,
        ).order_by(
            Lead.last_contacted_at,
            Lead.lead_score.desc(),
        ).limit(limit).all(),
        "unanswered_replies": EmailReply.query.filter(
            EmailReply.lead_id.isnot(None),
            EmailReply.reply_sent_at.is_(None),
        ).order_by(
            EmailReply.received_at.desc(),
        ).limit(limit).all(),
    }


@main_bp.route("/dashboard")
def dashboard():
    return render_template(
        "dashboard.html",
        stats=dashboard_metrics(),
        queues=dashboard_work_queues(),
    )


def company_contacts_by_type(company, contact_type):
    """Vráti kontakty firmy zoradené podľa primárnosti a confidence."""
    return sorted(
        (
            contact
            for contact in company.contacts
            if contact.contact_type == contact_type
        ),
        key=lambda contact: (
            bool(contact.is_primary),
            bool(contact.is_verified),
            contact.confidence_score or 0,
        ),
        reverse=True,
    )


@main_bp.route("/companies")
def companies():
    filters = company_filters_from_source(request.args)
    selected_campaign_id = request.args.get("campaign_id", type=int)
    active_filters = {name: value for name, value in filters.items() if value}
    if selected_campaign_id:
        active_filters["campaign_id"] = selected_campaign_id
    filter_error = None
    try:
        query = filtered_companies_query(filters)
    except LocationLookupError as exc:
        filter_error = str(exc)
        query = Company.query.filter(false())

    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1

    filtered_companies_count = query.count()
    page_count = max(
        1,
        (filtered_companies_count + COMPANIES_PER_PAGE - 1)
        // COMPANIES_PER_PAGE,
    )
    page = max(1, min(page, page_count))

    companies = query.order_by(
        Company.outreach_relevant.desc(),
        Company.subcontractor_need.desc(),
        Company.official_name,
        Company.ico,
    ).offset(
        (page - 1) * COMPANIES_PER_PAGE
    ).limit(
        COMPANIES_PER_PAGE
    ).all()

    return render_template(
        "companies.html",
        companies=companies,
        total_companies=Company.query.count(),
        filtered_companies_count=filtered_companies_count,
        page=page,
        page_count=page_count,
        page_size=COMPANIES_PER_PAGE,
        active_filters=active_filters,
        filters=filters,
        filter_error=filter_error,
        campaigns=Campaign.query.filter(
            Campaign.status.notin_(["completed", "archived"])
        ).order_by(Campaign.created_at.desc()).all(),
        selected_campaign_id=selected_campaign_id,
    )


@main_bp.route("/companies/enrich-contacts", methods=["POST"])
def enrich_filtered_company_contacts():
    """Vyhľadá kontakty len pre firmy vybrané filtrom v prehľade."""
    filters = company_filters_from_source(request.form)

    try:
        batch_size = int(request.form.get("batch_size", 25))
    except ValueError:
        batch_size = 25

    batch_size = max(1, min(batch_size, 100))
    try:
        query = filtered_companies_query(filters)
    except LocationLookupError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.companies", **filters))

    selected_companies = query.filter(
        Company.contacts_checked_at.is_(None),
        ~Company.contacts.any(),
    ).order_by(
        Company.official_name,
        Company.ico,
    ).limit(batch_size).all()

    if not selected_companies:
        flash(
            "Pre zvolený filter sa nenašli nové firmy na hľadanie kontaktov.",
            "error",
        )
    else:
        summary = enrich_company_contacts(
            companies=selected_companies,
            delay_seconds=0.5,
        )
        flash(
            "Hľadanie kontaktov: "
            f"{summary['companies_with_contacts']} firiem s kontaktom, "
            f"{summary['companies_without_contacts']} bez kontaktu, "
            f"{len(summary['errors'])} chýb.",
            "success" if not summary["errors"] else "error",
        )

    query_params = {
        name: value
        for name, value in filters.items()
        if value
    }
    return redirect(url_for("main.companies", **query_params))


@main_bp.route("/companies/enrich-financials", methods=["POST"])
def enrich_filtered_company_financials():
    """Načíta financie z RÚZ iba pre firmy vybrané aktuálnym filtrom."""
    filters = company_filters_from_source(request.form)
    try:
        batch_size = int(request.form.get("batch_size", 10))
    except ValueError:
        batch_size = 10

    batch_size = max(1, min(batch_size, 25))
    include_existing = request.form.get("include_existing") == "1"
    try:
        query = filtered_companies_query(filters).filter(Company.ico.isnot(None))
    except LocationLookupError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.companies", **filters))
    if not include_existing:
        query = query.filter(Company.financials_checked_at.is_(None))

    selected_companies = query.order_by(
        Company.official_name,
        Company.ico,
    ).limit(batch_size).all()

    if not selected_companies:
        flash(
            "Pre zvolený filter sa nenašli nové firmy na finančný enrichment.",
            "error",
        )
    else:
        summary = enrich_company_financials(
            companies=selected_companies,
            delay_seconds=0.1,
        )
        flash(
            "RÚZ financie: "
            f"{summary['companies_with_financials']} firiem s údajmi, "
            f"{summary['companies_without_financials']} bez údajov, "
            f"{len(summary['errors'])} chýb.",
            "success" if not summary["errors"] else "error",
        )

    query_params = {name: value for name, value in filters.items() if value}
    return redirect(url_for("main.companies", **query_params))


@main_bp.route("/companies/analyze-websites", methods=["POST"])
def analyze_filtered_company_websites():
    """Analyzuje malú dávku firiem, ktorú používateľ vybral filtrami."""
    filters = company_filters_from_source(request.form)

    try:
        batch_size = int(request.form.get("batch_size", 10))
    except ValueError:
        batch_size = 10

    batch_size = max(1, min(batch_size, 25))
    include_analyzed = request.form.get("include_analyzed") == "1"
    try:
        query = filtered_companies_query(filters).filter(
            Company.contacts.any(CompanyContact.contact_type == "website")
        )
    except LocationLookupError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.companies", **filters))

    if not include_analyzed:
        query = query.filter(Company.website_analyzed_at.is_(None))

    selected_companies = query.order_by(
        Company.official_name,
        Company.ico,
    ).limit(batch_size).all()

    analyzed_companies = 0
    errors = 0

    for company in selected_companies:
        try:
            enrich_company_from_website(company)
            db.session.commit()
            analyzed_companies += 1
        except Exception:
            db.session.rollback()
            errors += 1

    if not selected_companies:
        flash(
            "Pre zvolený filter sa nenašli firmy s webom pripravené na analýzu.",
            "error",
        )
    else:
        flash(
            f"Webová analýza: {analyzed_companies} úspešne, {errors} chýb.",
            "success" if analyzed_companies else "error",
        )

    query_params = {
        name: value
        for name, value in filters.items()
        if value
    }
    return redirect(url_for("main.companies", **query_params))


@main_bp.route("/companies/<int:company_id>")
def company_detail(company_id):
    company = Company.query.get_or_404(company_id)
    from services.website_presence import website_presence_summary

    return render_template(
        "company_detail.html",
        company=company,
        website_presence=website_presence_summary(company),
        email_contacts=company_contacts_by_type(company, "email"),
        outreach_lead=Lead.query.filter_by(company_id=company.id).one_or_none(),
        outreach_industries=OUTREACH_INDUSTRIES,
        default_follow_up=(date.today() + timedelta(days=5)).isoformat(),
    )


@main_bp.route(
    "/companies/<int:company_id>/enrich-contacts",
    methods=["POST"],
)
def enrich_single_company_contacts(company_id):
    """Vyhľadá alebo obnoví kontakty jednej firmy cez Brave."""
    company = Company.query.get_or_404(company_id)
    summary = enrich_company_contacts(
        companies=[company],
        delay_seconds=0,
    )

    if summary["errors"]:
        flash("Kontakty sa nepodarilo načítať. Podrobnosti sú v serverovom logu.", "error")
    elif summary["companies_with_contacts"]:
        flash(
            "Hľadanie kontaktov bolo dokončené: "
            f"{summary['saved_contacts']} vybraných kontaktov.",
            "success",
        )
    else:
        flash("Pre firmu sa nenašli použiteľné kontakty.", "error")

    return redirect(url_for("main.company_detail", company_id=company.id))


@main_bp.route(
    "/companies/<int:company_id>/enrich-financials",
    methods=["POST"],
)
def enrich_single_company_financials(company_id):
    """Načíta alebo obnoví finančné údaje jednej firmy z RÚZ."""
    company = Company.query.get_or_404(company_id)
    if not company.ico:
        flash("Firma nemá IČO potrebné na vyhľadanie v RÚZ.", "error")
        return redirect(url_for("main.company_detail", company_id=company.id))

    summary = enrich_company_financials(
        companies=[company],
        delay_seconds=0,
    )
    if summary["errors"]:
        flash("RÚZ financie sa nepodarilo načítať. Podrobnosti sú v serverovom logu.", "error")
    elif summary["companies_with_financials"]:
        flash(
            f"Finančné údaje za rok {company.financial_year} boli načítané.",
            "success",
        )
    else:
        flash("RÚZ nemá pre firmu podporované verejné finančné údaje.", "error")

    return redirect(url_for("main.company_detail", company_id=company.id))


@main_bp.route(
    "/companies/<int:company_id>/generate-outreach",
    methods=["POST"],
)
def generate_company_outreach(company_id):
    """Vytvorí editovateľný AI návrh oslovenia pre zvolené odvetvie."""
    company = Company.query.get_or_404(company_id)
    email = request.form.get("email", "").strip().lower()
    industry = request.form.get("industry", "").strip()
    custom_industry = request.form.get("custom_industry", "").strip()
    work_type = custom_industry if industry == "Vlastné" else industry
    available_emails = {
        contact.value.strip().lower()
        for contact in company_contacts_by_type(company, "email")
    }

    if email not in available_emails:
        flash("Vyber e-mail uložený pri tejto firme.", "error")
    elif not work_type:
        flash("Vyber alebo zadaj odvetvie pre oslovenie.", "error")
    elif len(work_type) > 100:
        flash("Odvetvie môže mať najviac 100 znakov.", "error")
    else:
        try:
            lead = get_or_create_company_lead(company, email, work_type)
            lead.suggested_message = generate_lead_message(lead)
            if not lead.status or lead.status == "Nový":
                lead.status = "Osloviť"
            db.session.add(
                LeadActivity(
                    lead=lead,
                    activity_type="Poznámka",
                    note=(
                        "AI vygenerovala návrh oslovenia pre odvetvie: "
                        f"{work_type}."
                    ),
                )
            )
            db.session.commit()
            flash("Návrh oslovenia bol vygenerovaný. Pred odoslaním ho skontroluj.", "success")
        except Exception:
            current_app.logger.exception("Company outreach generation failed")
            db.session.rollback()
            flash("Generovanie oslovenia zlyhalo. Podrobnosti sú v serverovom logu.", "error")

    return redirect(url_for("main.company_detail", company_id=company.id))


@main_bp.route("/companies/<int:company_id>/send-outreach", methods=["POST"])
def send_company_outreach(company_id):
    """Odošle prvé oslovenie z detailu firmy a založí CRM follow-up."""
    company = Company.query.get_or_404(company_id)
    email = request.form.get("email", "").strip().lower()
    blocked = _manual_send_blocker(company, email)
    if blocked:
        flash(blocked, "error")
        return redirect(url_for("main.company_detail", company_id=company.id))
    subject = normalize_email_subject(request.form.get("subject", ""))
    message_text = request.form.get("message", "").strip()
    industry = request.form.get("industry", "").strip()
    custom_industry = request.form.get("custom_industry", "").strip()
    work_type = custom_industry if industry == "Vlastné" else industry
    follow_up_raw = request.form.get("follow_up_at", "").strip()
    available_emails = {
        contact.value.strip().lower()
        for contact in company_contacts_by_type(company, "email")
    }

    if email not in available_emails:
        flash("Vyber e-mail uložený pri tejto firme.", "error")
        return redirect(url_for("main.company_detail", company_id=company.id))

    if not subject or not message_text:
        flash("Predmet aj text e-mailu sú povinné.", "error")
        return redirect(url_for("main.company_detail", company_id=company.id))

    follow_up_at = None
    if follow_up_raw:
        try:
            follow_up_at = datetime.combine(
                date.fromisoformat(follow_up_raw),
                datetime.min.time(),
            )
        except ValueError:
            flash("Neplatný dátum follow-upu.", "error")
            return redirect(url_for("main.company_detail", company_id=company.id))

    try:
        lead = get_or_create_company_lead(company, email, work_type)
        message = Message(subject=subject, recipients=[email], body=message_text)
        send_profile_message(message)

        lead.suggested_message = message_text
        lead.status = "Oslovený"
        lead.last_contacted_at = datetime.utcnow()
        lead.next_follow_up_at = follow_up_at
        db.session.add(
            LeadActivity(
                lead=lead,
                activity_type="Email odoslaný",
                note=f"Predmet: {subject}\n\n{message_text}",
            )
        )
        db.session.add(
            OutboundEmail(
                lead=lead,
                message_id=message.msgId,
                recipient=email,
                subject=subject,
                body=message_text,
            )
        )
        db.session.commit()
    except Exception:
        current_app.logger.exception("Company outreach delivery failed")
        db.session.rollback()
        flash("E-mail sa nepodarilo bezpečne odoslať. Skontroluj odoslanú poštu.", "error")
        return redirect(url_for("main.company_detail", company_id=company.id))

    flash("E-mail bol odoslaný a follow-up je uložený v CRM.", "success")
    return redirect(url_for("main.company_detail", company_id=company.id))


@main_bp.route("/companies/<int:company_id>/analyze-website", methods=["POST"])
def analyze_company_website(company_id):
    company = Company.query.get_or_404(company_id)

    try:
        result = enrich_company_from_website(company)
        db.session.commit()
        flash(
            f"Web bol analyzovaný z {result['pages']} stránok.",
            "success",
        )
    except Exception:
        current_app.logger.exception("Company website analysis failed")
        db.session.rollback()
        flash("Analýza webu zlyhala. Podrobnosti sú v serverovom logu.", "error")

    return redirect(url_for("main.company_detail", company_id=company.id))

@main_bp.route("/", methods=["GET", "POST"])
def home():
    today = date.today()

    if request.method == "POST":
        company_name = request.form.get("company_name", "").strip()
        raw_website = request.form.get("website", "").strip()
        website = normalize_http_url(raw_website) if raw_website else None
        raw_email = request.form.get("email", "").strip()
        email = normalize_valid_email(raw_email) if raw_email else None

        if not company_name:
            flash("Názov firmy je povinný.", "error")
            return redirect(url_for("main.home"))
        if raw_website and website is None:
            flash("Web musí byť platná verejná HTTP alebo HTTPS adresa.", "error")
            return redirect(url_for("main.home"))
        if raw_email and email is None:
            flash("E-mail nemá platný formát.", "error")
            return redirect(url_for("main.home"))

        lead_score_raw = request.form.get("lead_score", "3")

        try:
            lead_score = int(lead_score_raw)
        except ValueError:
            lead_score = 3

        lead_score = max(1, min(5, lead_score))

        lead = Lead(
            company_name=company_name,
            website=website,
            email=email,
            phone=request.form.get("phone", "").strip(),
            city=request.form.get("city", "").strip(),
            country=request.form.get("country", "Slovensko").strip() or "Slovensko",
            company_segment=request.form.get("company_segment", "").strip(),
            work_type=request.form.get("work_type", "").strip(),
            work_subtype=request.form.get("work_subtype", "").strip(),
            lead_score=lead_score,
            status=request.form.get("status", "Nový").strip(),
            reason_to_contact=request.form.get("reason_to_contact", "").strip(),
            ai_summary=request.form.get("ai_summary", "").strip(),
            suggested_message=request.form.get("suggested_message", "").strip(),
        )

        db.session.add(lead)
        db.session.commit()

        flash("Lead bol pridaný.", "success")
        return redirect(url_for("main.home"))

    status_filter = request.args.get("status", "").strip()
    work_type_filter = request.args.get("work_type", "").strip()
    score_filter = request.args.get("score", "").strip()
    follow_up_filter = request.args.get("follow_up", "").strip()

    query = Lead.query

    if status_filter:
        query = query.filter(Lead.status == status_filter)

    if work_type_filter:
        query = query.filter(Lead.work_type == work_type_filter)

    if score_filter:
        try:
            min_score = int(score_filter)
            query = query.filter(Lead.lead_score >= min_score)
        except ValueError:
            pass

    if follow_up_filter == "today":
        query = query.filter(
            Lead.next_follow_up_at.isnot(None),
            db.func.date(Lead.next_follow_up_at) <= today
        )

    leads = query.order_by(Lead.created_at.desc()).all()
    last_activities = {}

    for lead in leads:
        last_activity = LeadActivity.query.filter_by(lead_id=lead.id)\
            .order_by(LeadActivity.created_at.desc())\
            .first()

        last_activities[lead.id] = last_activity

    email_sent_count = LeadActivity.query.filter_by(activity_type="Email odoslaný").count()
    reply_count = LeadActivity.query.filter_by(activity_type="Odpoveď").count()
    inspection_count = LeadActivity.query.filter_by(activity_type="Obhliadka").count()

    if email_sent_count > 0:
        reply_rate = round((reply_count / email_sent_count) * 100, 1)
    else:
        reply_rate = 0

    stats = {
        "total": Lead.query.count(),
        "new": Lead.query.filter_by(status="Nový").count(),
        "to_contact": Lead.query.filter_by(status="Osloviť").count(),
        "contacted": Lead.query.filter_by(status="Oslovený").count(),
        "won": Lead.query.filter_by(status="Vyhraté").count(),
        "follow_up_today": Lead.query.filter(
            Lead.next_follow_up_at.isnot(None),
            db.func.date(Lead.next_follow_up_at) <= today
        ).count(),
        "emails_sent": email_sent_count,
        "replies": reply_count,
        "inspections": inspection_count,
        "reply_rate": reply_rate,
    }

    return render_template(
        "home.html",
        leads=leads,
        stats=stats,
        work_types=WORK_TYPES,
        company_segments=COMPANY_SEGMENTS,
        lead_statuses=LEAD_STATUSES,
        last_activities=last_activities,
        filters={
            "status": status_filter,
            "work_type": work_type_filter,
            "score": score_filter,
            "follow_up": follow_up_filter,
        }
    )

@main_bp.route("/lead/<int:lead_id>/delete", methods=["POST"])
def delete_lead(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    db.session.delete(lead)
    db.session.commit()

    flash("Lead bol vymazaný.", "success")
    return redirect(url_for("main.home"))


@main_bp.route("/lead/<int:lead_id>/status", methods=["POST"])
def update_lead_status(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    new_status = request.form.get("status", "Nový")

    if new_status in LEAD_STATUSES:
        lead.status = new_status
        db.session.commit()
        flash("Stav leadu bol upravený.", "success")
    else:
        flash("Neplatný stav leadu.", "error")

    return redirect(url_for("main.home"))

@main_bp.route("/lead/<int:lead_id>/generate-message", methods=["POST"])
def generate_message(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    try:
        message = generate_lead_message(lead)
        lead.suggested_message = message
        activity = LeadActivity(
            lead_id=lead.id,
            activity_type="Poznámka",
            note="AI vygenerovala návrh oslovenia."
        )

        db.session.add(activity)

        if not lead.status or lead.status == "Nový":
            lead.status = "Osloviť"

        db.session.commit()
        flash("Oslovenie bolo vygenerované.", "success")

    except Exception:
        current_app.logger.exception("Lead outreach generation failed")
        db.session.rollback()
        flash("Generovanie oslovenia zlyhalo. Podrobnosti sú v serverovom logu.", "error")

    return redirect(_safe_referrer(url_for("main.home")))

@main_bp.route("/lead/<int:lead_id>/save-message", methods=["POST"])
def save_message(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    message = request.form.get("suggested_message", "").strip()
    raw_subject = request.form.get("email_subject", "")
    subject = normalize_email_subject(raw_subject) if raw_subject.strip() else None

    if not message:
        flash("Text správy nemôže byť prázdny.", "error")
        return redirect(url_for("main.home"))

    lead.suggested_message = message
    if raw_subject.strip() and subject is None:
        flash("Predmet musí byť jeden platný riadok s najviac 255 znakmi.", "error")
        return redirect(_safe_referrer(url_for("main.home")))

    lead.suggested_subject = subject or lead.suggested_subject
    db.session.commit()

    flash("Text oslovenia bol uložený.", "success")
    return redirect(_safe_referrer(url_for("main.home")))


@main_bp.route("/lead/<int:lead_id>/send-email", methods=["POST"])
def send_email(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    blocked = _manual_send_blocker(lead.company, lead.email, lead=lead)
    if blocked:
        flash(blocked, "error")
        return redirect(url_for("main.lead_detail", lead_id=lead.id))

    message_text = request.form.get("suggested_message", "").strip()
    raw_subject = request.form.get("email_subject", "")
    subject = normalize_email_subject(raw_subject) if raw_subject.strip() else None

    if not lead.email:
        flash("Lead nemá vyplnený email.", "error")
        return redirect(url_for("main.home"))

    if not message_text:
        flash("Text emailu nemôže byť prázdny.", "error")
        return redirect(_safe_referrer(url_for("main.home")))

    if raw_subject.strip() and subject is None:
        flash("Predmet musí byť jeden platný riadok s najviac 255 znakmi.", "error")
        return redirect(_safe_referrer(url_for("main.home")))
    if not subject:
        subject = "Možnosť spolupráce"

    try:
        msg = Message(
            subject=subject,
            recipients=[lead.email],
            body=message_text
        )

        send_profile_message(msg)

        lead.suggested_message = message_text
        lead.suggested_subject = subject
        lead.status = "Oslovený"
        lead.last_contacted_at = datetime.utcnow()

        activity = LeadActivity(
            lead_id=lead.id,
            activity_type="Email odoslaný",
            note=f"Predmet: {subject}\n\n{message_text}"
        )

        db.session.add(activity)
        db.session.add(
            OutboundEmail(
                lead=lead,
                message_id=msg.msgId,
                recipient=lead.email,
                subject=subject,
                body=message_text,
            )
        )
        db.session.commit()

        flash("Email bol odoslaný a lead označený ako oslovený.", "success")

    except Exception:
        current_app.logger.exception("Legacy lead delivery failed")
        db.session.rollback()
        flash("E-mail sa nepodarilo bezpečne odoslať. Skontroluj odoslanú poštu.", "error")

    return redirect(url_for("main.home"))

@main_bp.route("/find-leads", methods=["POST"])
def find_leads():
    locations_raw = request.form.get("locations", "")
    company_types_raw = request.form.get("company_types", "")
    work_type = request.form.get("work_type", "").strip()
    work_subtype = request.form.get("work_subtype", "").strip()
    max_results_raw = request.form.get("max_results", "5")

    try:
        max_results = int(max_results_raw)
    except ValueError:
        max_results = 5

    max_results = max(1, min(20, max_results))

    locations = [item.strip() for item in locations_raw.split(",") if item.strip()]
    company_types = [item.strip() for item in company_types_raw.split(",") if item.strip()]

    if not locations:
        flash("Zadaj aspoň jednu lokalitu.", "error")
        return redirect(url_for("main.home"))

    if not company_types:
        flash("Zadaj aspoň jeden typ firmy.", "error")
        return redirect(url_for("main.home"))

    queries = build_search_queries(locations, company_types)

    created_count = 0
    skipped_count = 0

    for query in queries:
        try:
            places = search_places_text(query, max_results=max_results)

            for place in places:
                google_place_id = place.get("google_place_id")

                if not google_place_id:
                    skipped_count += 1
                    continue

                existing = Lead.query.filter_by(google_place_id=google_place_id).first()

                if existing:
                    skipped_count += 1
                    continue

                lead = Lead(
                    google_place_id=google_place_id,
                    company_name=place.get("company_name") or "Neznáma firma",
                    website=normalize_http_url(place.get("website")),
                    phone=place.get("phone"),
                    address=place.get("address"),
                    business_status=place.get("business_status"),
                    source="Google Places",
                    city=", ".join(locations),
                    country="Slovensko",
                    company_segment=query,
                    work_type=work_type,
                    work_subtype=work_subtype,
                    status="Nový",
                    lead_score=3,
                    reason_to_contact=(
                        f"Firma bola nájdená podľa dotazu: {query}. "
                        f"Skontrolovať, či dáva zmysel osloviť ju s ponukou: {work_type} / {work_subtype}."
                    )
                )

                db.session.add(lead)
                created_count += 1

        except Exception:
            current_app.logger.exception("Lead search failed for query %r", query)
            flash(f"Hľadanie pre dotaz '{query}' zlyhalo.", "error")

    db.session.commit()

    flash(f"Hľadanie dokončené. Pridané: {created_count}, preskočené duplicity/neplatné: {skipped_count}.", "success")
    return redirect(url_for("main.home"))

@main_bp.route("/lead/<int:lead_id>", methods=["GET"])
def lead_detail(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    activities = LeadActivity.query.filter_by(lead_id=lead.id)\
        .order_by(LeadActivity.created_at.desc())\
        .all()
    
    email_replies = EmailReply.query.filter_by(lead_id=lead.id)\
        .order_by(EmailReply.created_at.desc())\
        .all()

    quote_requests = QuoteRequest.query.filter_by(lead_id=lead.id)\
        .order_by(QuoteRequest.created_at.desc())\
        .all()
    
    return render_template(
        "lead_detail.html",
        lead=lead,
        activities=activities,
        activity_types=ACTIVITY_TYPES,
        work_types=WORK_TYPES,
        company_segments=COMPANY_SEGMENTS,
        lead_statuses=LEAD_STATUSES,
        email_replies=email_replies,
        quote_requests=quote_requests,
    )


@main_bp.route("/lead/<int:lead_id>/quote-request", methods=["POST"])
def add_quote_request(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    reply_id = request.form.get("reply_id", type=int)
    email_reply = EmailReply.query.filter_by(id=reply_id, lead_id=lead.id).first()
    if email_reply is None:
        flash("Vyber platnú odpoveď zákazníka.", "error")
        return redirect(url_for("main.lead_detail", lead_id=lead.id))

    try:
        _, created = create_quote_request(lead, email_reply)
        flash(
            "Požiadavka čaká na cenu."
            if created
            else "Táto odpoveď už má požiadavku na cenu.",
            "success" if created else "warning",
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("main.lead_detail", lead_id=lead.id))


@main_bp.route(
    "/lead/<int:lead_id>/quote-request/<int:quote_request_id>/send",
    methods=["POST"],
)
def price_and_send_quote(lead_id, quote_request_id):
    quote_request = QuoteRequest.query.filter_by(
        id=quote_request_id,
        lead_id=lead_id,
    ).first_or_404()
    try:
        submit_price_and_send(
            quote_request,
            amount=request.form.get("amount", ""),
            currency=request.form.get("currency", "EUR"),
            unit=request.form.get("unit", ""),
            vat_text=request.form.get("vat_text", ""),
            terms=request.form.get("terms", ""),
            validity_days=request.form.get("validity_days", "14"),
            sender_signature=request.form.get("sender_signature", ""),
            authorized=request.form.get("action") == "send",
        )
        flash("Cenová ponuka bola odoslaná.", "success")
    except (PermissionError, ValueError) as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except Exception:
        flash(
            "Odoslanie nemá potvrdený výsledok. Pred ďalšou akciou skontroluj poštu.",
            "error",
        )
    return redirect(url_for("main.lead_detail", lead_id=lead_id))


@main_bp.route("/lead/<int:lead_id>/analyze", methods=["POST"])
def analyze_lead_route(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    try:
        data = analyze_lead(lead)

        lead.company_segment = data.get("company_segment") or lead.company_segment
        lead.work_type = data.get("work_type") or lead.work_type
        lead.work_subtype = data.get("work_subtype") or lead.work_subtype
        lead.reason_to_contact = data.get("reason_to_contact") or lead.reason_to_contact
        lead.ai_summary = data.get("ai_summary") or lead.ai_summary

        score = data.get("lead_score", lead.lead_score)

        try:
            score = int(score)
        except ValueError:
            score = lead.lead_score or 3

        lead.lead_score = max(1, min(5, score))

        if not lead.status or lead.status == "Nový":
            lead.status = "Skontrolovať"
        activity = LeadActivity(
            lead_id=lead.id,
            activity_type="Poznámka",
            note="Lead bol vyhodnotený cez AI."
        )

        db.session.add(activity)
        db.session.commit()
        flash("Lead bol vyhodnotený cez AI.", "success")

    except Exception:
        current_app.logger.exception("Lead AI analysis failed")
        db.session.rollback()
        flash("AI vyhodnotenie leadu zlyhalo. Podrobnosti sú v serverovom logu.", "error")

    return redirect(url_for("main.lead_detail", lead_id=lead.id))

@main_bp.route("/lead/<int:lead_id>/update-basic", methods=["POST"])
def update_lead_basic(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    lead.email = request.form.get("email", "").strip()
    lead.phone = request.form.get("phone", "").strip()
    lead.company_segment = request.form.get("company_segment", "").strip()
    lead.work_type = request.form.get("work_type", "").strip()
    lead.work_subtype = request.form.get("work_subtype", "").strip()

    score_raw = request.form.get("lead_score", "3")

    try:
        score = int(score_raw)
    except ValueError:
        score = 3

    lead.lead_score = max(1, min(5, score))

    db.session.commit()

    flash("Lead bol upravený.", "success")
    return redirect(_safe_referrer(url_for("main.lead_detail", lead_id=lead.id)))


@main_bp.route("/lead/<int:lead_id>/set-follow-up", methods=["POST"])
def set_follow_up(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    follow_up_date_raw = request.form.get("next_follow_up_at", "").strip()

    if not follow_up_date_raw:
        lead.next_follow_up_at = None
        db.session.commit()
        flash("Follow-up bol odstránený.", "success")
        return redirect(_safe_referrer(url_for("main.home")))

    try:
        follow_up_date = datetime.strptime(follow_up_date_raw, "%Y-%m-%d")
    except ValueError:
        flash("Neplatný dátum follow-upu.", "error")
        return redirect(_safe_referrer(url_for("main.home")))

    lead.next_follow_up_at = follow_up_date
    activity = LeadActivity(
        lead_id=lead.id,
        activity_type="Follow-up nastavený",
        note=f"Follow-up nastavený na {follow_up_date.strftime('%d.%m.%Y')}"
    )

    db.session.add(activity)

    if lead.status in ["Nový", "Oslovený"]:
        lead.status = "Oslovený"

    db.session.commit()

    flash("Follow-up bol nastavený.", "success")
    return redirect(_safe_referrer(url_for("main.home")))

@main_bp.route("/lead/<int:lead_id>/add-activity", methods=["POST"])
def add_activity(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    activity_type = request.form.get("activity_type", "Poznámka").strip()
    note = request.form.get("note", "").strip()

    if not note:
        flash("Poznámka aktivity nemôže byť prázdna.", "error")
        return redirect(url_for("main.lead_detail", lead_id=lead.id))

    if activity_type not in ACTIVITY_TYPES:
        activity_type = "Poznámka"

    activity = LeadActivity(
        lead_id=lead.id,
        activity_type=activity_type,
        note=note
    )

    db.session.add(activity)
    db.session.commit()

    flash("Aktivita bola pridaná.", "success")
    return redirect(url_for("main.lead_detail", lead_id=lead.id))




@main_bp.route("/lead/<int:lead_id>/check-reply", methods=["POST"])
def check_reply(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    profile_ids = [
        profile_id
        for profile_id, in db.session.query(OutboundEmail.sender_profile_id)
        .filter(
            OutboundEmail.lead_id == lead.id,
            OutboundEmail.sender_profile_id.isnot(None),
        )
        .distinct()
        .order_by(OutboundEmail.sender_profile_id)
        .all()
    ]
    if profile_ids:
        totals = {"imported": 0, "reconciled": 0, "cancelled": 0}
        try:
            for profile_id in profile_ids:
                profile = db.session.get(SenderProfile, profile_id)
                if profile is None:
                    raise RuntimeError("Pôvodný profil schránky už nie je dostupný.")
                result = sync_profile_inbox(profile)
                for key in totals:
                    totals[key] += int(result.get(key, 0))
        except Exception:
            db.session.rollback()
            flash(
                "Kontrola profilovej schránky nebola úplná; žiadny follow-up sa preto neodošle.",
                "error",
            )
            return redirect(
                _safe_referrer(url_for("main.lead_detail", lead_id=lead.id))
            )

        if totals["imported"] or totals["reconciled"]:
            flash(
                "Profilová schránka bola skontrolovaná a odpovede sa bezpečne priradili.",
                "success",
            )
        else:
            flash("V profilovej schránke nebola nájdená nová odpoveď.", "info")
        return redirect(
            _safe_referrer(url_for("main.lead_detail", lead_id=lead.id))
        )

    if not lead.email:
        flash("Lead nemá email, nemám podľa čoho hľadať odpoveď.", "error")
        return redirect(_safe_referrer(url_for("main.lead_detail", lead_id=lead.id)))

    try:
        reply = check_reply_from_sender(
            sender_email=lead.email,
            after_datetime=lead.last_contacted_at,
            outbound_message_ids=[
                outbound_email.message_id
                for outbound_email in lead.outbound_emails
            ],
        )

        if not reply:
            flash("Zatiaľ som nenašiel odpoveď od tejto firmy.", "error")
            return redirect(_safe_referrer(url_for("main.lead_detail", lead_id=lead.id)))

        subject = reply.get("subject", "")
        received_at = reply.get("received_at")
        body = reply.get("body", "")
        imap_message_id = reply.get("message_id")

        if (
            imap_message_id
            and EmailReply.query.filter_by(
                imap_message_id=imap_message_id
            ).first()
        ):
            flash("Táto odpoveď už je uložená v CRM.", "info")
            return redirect(
                _safe_referrer(url_for("main.lead_detail", lead_id=lead.id))
            )

        note = f"Predmet: {subject}\n"

        if received_at:
            note += f"Doručené: {received_at.strftime('%d.%m.%Y %H:%M')}\n"

        note += f"\n{body}"

        activity = LeadActivity(
            lead_id=lead.id,
            activity_type="Odpoveď",
            note=note
        )

        from_name, from_email = parseaddr(reply.get("from", ""))
        from_email = normalize_valid_email(from_email or lead.email)
        if not from_email:
            raise ValueError("Odpoveď nemá platnú adresu odosielateľa.")
        email_reply = EmailReply(
            lead_id=lead.id,
            campaign_recipient=campaign_recipient_for_message_ids(
                reply.get("thread_message_ids"),
                lead=lead,
                sender_email=from_email or lead.email,
            ),
            from_email=from_email,
            from_name=from_name or None,
            subject=subject,
            text_body=body,
            received_at=received_at or datetime.utcnow(),
            imap_message_id=imap_message_id,
        )
        db.session.add(email_reply)

        lead.status = "Odpovedal"
        lead.next_follow_up_at = None
        suppression = mark_campaign_recipient_replied(
            email_reply.campaign_recipient,
            email_reply.received_at,
            reply_body=email_reply.text_body,
            sender_email=email_reply.from_email,
        )

        db.session.add(activity)
        if suppression is not None:
            db.session.add(suppression)
        db.session.commit()

        flash("Našiel som odpoveď a zapísal ju do histórie.", "success")

    except Exception:
        current_app.logger.exception("Lead inbox check failed")
        db.session.rollback()
        flash("Kontrola odpovede zlyhala; nič sa neodoslalo.", "error")

    return redirect(_safe_referrer(url_for("main.lead_detail", lead_id=lead.id)))


@main_bp.route("/lead/<int:lead_id>/find-email", methods=["POST"])
def find_lead_email(lead_id):
    lead = Lead.query.get_or_404(lead_id)

    if lead.email:
        flash("Lead už má email.", "info")
        return redirect(url_for("main.home"))

    if not lead.website:
        flash("Lead nemá web, email sa nedá automaticky hľadať.", "warning")
        return redirect(url_for("main.home"))

    best_email, all_emails = find_email_on_website(lead.website)

    if best_email:
        lead.email = best_email

        if hasattr(lead, "ai_summary"):
            existing_summary = lead.ai_summary or ""
            lead.ai_summary = (
                existing_summary
                + f"\n\nNájdené emaily: {', '.join(all_emails)}"
            ).strip()

        db.session.commit()

        flash(f"Email nájdený: {best_email}", "success")
    else:
        flash("Email sa na webe nepodarilo nájsť.", "warning")

    return redirect(url_for("main.home"))



@main_bp.route("/leads/find-missing-emails", methods=["POST"])
def find_missing_emails():
    leads = Lead.query.filter(
        Lead.email.is_(None),
        Lead.website.isnot(None)
    ).limit(20).all()

    found_count = 0

    for lead in leads:
        best_email, all_emails = find_email_on_website(lead.website)

        if best_email:
            lead.email = best_email
            found_count += 1

            if hasattr(lead, "ai_summary"):
                existing_summary = lead.ai_summary or ""
                lead.ai_summary = (
                    existing_summary
                    + f"\n\nNájdené emaily: {', '.join(all_emails)}"
                ).strip()

    db.session.commit()

    flash(f"Nájdených emailov: {found_count}", "success")
    return redirect(url_for("main.home"))




@main_bp.route("/reply/<int:reply_id>/generate", methods=["POST"])
def generate_reply(reply_id):
    reply = EmailReply.query.get_or_404(reply_id)

    if not reply.lead:
        flash("Odpoveď nie je priradená k žiadnemu leadu.", "error")
        return redirect(url_for("main.home"))
    if reply.reply_sent_at or reply.reply_delivery_status is not None:
        flash(
            "K tejto odpovedi už prebehlo odoslanie alebo treba skontrolovať jeho výsledok.",
            "error",
        )
        return redirect(url_for("main.lead_detail", lead_id=reply.lead.id))

    draft = generate_reply_to_customer(reply.lead, reply)

    reply.ai_reply_draft = draft
    db.session.commit()

    flash("Návrh odpovede bol vygenerovaný.", "success")
    return redirect(url_for("main.lead_detail", lead_id=reply.lead.id))





@main_bp.route("/reply/<int:reply_id>/send", methods=["POST"])
def send_reply(reply_id):
    reply = EmailReply.query.get_or_404(reply_id)
    lead_id = reply.lead_id
    try:
        send_manual_reply(
            reply.id,
            request.form.get("reply_body"),
            authorized=True,
        )
    except (ManualReplySendError, SenderProfileError) as exc:
        db.session.rollback()
        flash(f"Nepodarilo sa odoslať odpoveď: {exc}", "error")
        return redirect(
            url_for("main.lead_detail", lead_id=lead_id)
            if lead_id else url_for("main.home")
        )
    except Exception:
        db.session.rollback()
        flash("Nepodarilo sa bezpečne spracovať odoslanie odpovede.", "error")
        return redirect(
            url_for("main.lead_detail", lead_id=lead_id)
            if lead_id else url_for("main.home")
        )

    flash("Odpoveď bola odoslaná.", "success")
    return redirect(url_for("main.lead_detail", lead_id=lead_id))




@main_bp.route("/lead/<int:lead_id>/sync-hubspot", methods=["POST"])
def sync_lead_hubspot(lead_id):
    """Synchronizuje CRM až po samostatnom potvrdení používateľa."""
    lead = Lead.query.get_or_404(lead_id)
    reply_id = request.form.get("reply_id", type=int)
    reply = None
    if reply_id:
        reply = EmailReply.query.filter_by(id=reply_id, lead_id=lead.id).first_or_404()

    note_body = None
    if reply is not None:
        note_body = (
            f"Prijatá odpoveď: {reply.subject or 'Bez predmetu'}\n\n"
            f"{reply.text_body or reply.html_body or 'Bez textu'}"
        )
        if reply.reply_sent_at:
            note_body += "\n\nOdpoveď používateľa bola odoslaná."
        elif reply.ai_reply_draft:
            note_body += "\n\nAI návrh čaká na schválenie používateľa."

    try:
        sync_lead_to_hubspot(lead, note_body=note_body)
        db.session.commit()
        flash("Lead bol synchronizovaný do HubSpotu.", "success")
    except HubSpotError:
        current_app.logger.exception("HubSpot lead synchronization failed")
        db.session.rollback()
        flash("HubSpot synchronizácia zlyhala. Podrobnosti sú v serverovom logu.", "error")

    return redirect(url_for("main.lead_detail", lead_id=lead.id))


@main_bp.route("/inbox")
def inbox():
    emails = EmailReply.query.filter(
        EmailReply.lead_id.isnot(None)
    ).order_by(EmailReply.received_at.desc()).all()

    return render_template(
        "inbox.html",
        emails=emails,
        selected_email=None,
        sender_profiles=SenderProfile.query.order_by(SenderProfile.id).all(),
    )


@main_bp.route("/inbox/<int:email_id>")
def inbox_detail(email_id):
    emails = EmailReply.query.filter(
        EmailReply.lead_id.isnot(None)
    ).order_by(EmailReply.received_at.desc()).all()
    selected_email = EmailReply.query.filter(
        EmailReply.id == email_id,
        EmailReply.lead_id.isnot(None),
    ).first_or_404()
    conversation_messages = []

    if selected_email.lead:
        for outbound_email in selected_email.lead.outbound_emails:
            conversation_messages.append({
                "direction": "outbound",
                "subject": outbound_email.subject,
                "body": outbound_email.body,
                "sender": "Ty",
                "recipient": outbound_email.recipient,
                "sent_at": outbound_email.sent_at,
            })

        for inbound_email in selected_email.lead.email_replies:
            conversation_messages.append({
                "direction": "inbound",
                "subject": inbound_email.subject,
                "body": inbound_email.text_body or inbound_email.html_body,
                "sender": inbound_email.from_name or inbound_email.from_email,
                "recipient": None,
                "sent_at": inbound_email.received_at or inbound_email.created_at,
            })

        conversation_messages.sort(
            key=lambda message: message["sent_at"] or datetime.min
        )

    return render_template(
        "inbox.html",
        emails=emails,
        selected_email=selected_email,
        conversation_messages=conversation_messages,
        sender_profiles=SenderProfile.query.order_by(SenderProfile.id).all(),
    )


@main_bp.route("/inbox/sync", methods=["POST"])
def sync_inbox():
    """Načíta posledné IMAP správy a uloží iba doteraz neznáme e-maily."""
    profile_id = request.form.get("sender_profile_id", "").strip()
    if profile_id:
        if not profile_id.isdigit():
            flash("Vyber platný profil schránky.", "error")
            return redirect(url_for("main.inbox"))
        profile = SenderProfile.query.get_or_404(int(profile_id))
        try:
            result = sync_profile_inbox(profile)
            flash(f"Inbox {profile.name}: nové odpovede {result['imported']}; zrušené pripomenutia {result['cancelled']}.", "success")
        except Exception:
            db.session.rollback()
            flash("Úplná kontrola profilu zlyhala; follow-upy zostávajú blokované.", "error")
        return redirect(url_for("main.inbox"))
    legacy_address = str(current_app.config.get("IMAP_USERNAME") or "").strip().casefold()
    # Merely preparing an unused identity must not disable the existing inbox.
    # Once a profile owns mail history, keep its account boundary even if disabled.
    if legacy_address and SenderProfile.query.filter(
        func.lower(func.trim(SenderProfile.sender_email)) == legacy_address,
        or_(
            OutboundEmail.query.filter(OutboundEmail.sender_profile_id == SenderProfile.id).exists(),
            EmailReply.query.filter(EmailReply.sender_profile_id == SenderProfile.id).exists(),
        ),
    ).first():
        flash("Táto schránka má vlastný profil. Vyber ho pri synchronizácii, aby sa zachovala identita odpovedí.", "error")
        return redirect(url_for("main.inbox"))
    try:
        messages = fetch_inbox_messages()
        imported_count = 0
        skipped_count = 0

        for message in messages:
            message_id = message["message_id"]

            if not message_id:
                skipped_count += 1
                continue

            if EmailReply.query.filter_by(imap_message_id=message_id).first():
                skipped_count += 1
                continue

            lead = Lead.query.filter(
                func.lower(func.trim(Lead.email)) == message["from_email"]
            ).first()

            if lead is None:
                skipped_count += 1
                continue

            recipient = campaign_recipient_for_message_ids(
                message.get("thread_message_ids"), lead=lead,
                sender_email=message["from_email"],
            )
            if recipient and any(item.sender_profile_id is not None for item in recipient.outbound_emails):
                # Explicit accounts are imported only by their own complete scan.
                skipped_count += 1
                continue

            reply = EmailReply(
                lead_id=lead.id,
                campaign_recipient=recipient,
                from_email=message["from_email"],
                from_name=message["from_name"],
                subject=message["subject"],
                text_body=message["body"],
                imap_message_id=message_id,
                received_at=message["received_at"] or datetime.utcnow(),
            )
            db.session.add(reply)

            lead.status = "Odpovedal"
            lead.next_follow_up_at = None
            suppression = mark_campaign_recipient_replied(
                reply.campaign_recipient,
                reply.received_at,
                reply_body=reply.text_body,
                sender_email=reply.from_email,
            )
            if suppression is not None:
                db.session.add(suppression)
            db.session.add(
                LeadActivity(
                    lead=lead,
                    activity_type="Odpoveď",
                    note=(
                        f"Predmet: {message['subject']}\n\n"
                        f"{message['body']}"
                    ),
                )
            )

            imported_count += 1

        db.session.commit()
        flash(
            f"Inbox: načítané {imported_count} nových správ, "
            f"preskočené {skipped_count} známych alebo neplatných.",
            "success",
        )
    except Exception:
        current_app.logger.exception("Legacy inbox synchronization failed")
        db.session.rollback()
        flash("Inbox sa nepodarilo bezpečne načítať; nič sa neodoslalo.", "error")

    return redirect(url_for("main.inbox"))


