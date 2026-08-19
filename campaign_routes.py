from datetime import datetime, timezone

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_mail import Message
from sqlalchemy import func

from extensions import db, mail
from models import (
    Campaign,
    CampaignRecipient,
    Company,
    CompanyContact,
    LeadActivity,
    OutboundEmail,
    Suppression,
)
from services.campaigns import (
    best_email_contact,
    ensure_opt_out_footer,
    is_suppressed,
    normalize_email,
    normalize_suppression_value,
    render_campaign_template,
)
from services.company_filtering import (
    company_filters_from_source,
    filtered_companies_query,
)
from services.crm import get_or_create_company_lead
from services.postal_locations import LocationLookupError


campaign_bp = Blueprint("campaigns", __name__, url_prefix="/campaigns")

OFFER_TYPES = {
    "service": "Služba",
    "collaboration": "Spolupráca",
    "product": "Produkt",
    "procurement": "Dopyt / hľadám dodávateľa",
}
CAMPAIGN_TERMINAL_STATUSES = {"completed", "archived"}
RECIPIENT_OUTCOMES = {
    "sent": "Odoslané",
    "replied": "Odpovedal",
    "interested": "Má záujem",
    "not_interested": "Nemá záujem",
    "bounced": "Nedoručené",
    "opted_out": "Neželá si kontakt",
}


def utcnow():
    return datetime.now(timezone.utc)


def naive_utcnow():
    return utcnow().replace(tzinfo=None)


def parse_limited_integer(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def add_suppression(scope, value, reason=None):
    normalized = normalize_suppression_value(scope, value)
    suppression = Suppression.query.filter_by(scope=scope, value=normalized).first()
    if suppression is None:
        suppression = Suppression(scope=scope, value=normalized)
        db.session.add(suppression)
    if reason:
        suppression.reason = reason[:500]
    return suppression


@campaign_bp.route("")
def list_campaigns():
    campaigns = Campaign.query.order_by(Campaign.created_at.desc()).all()
    return render_template(
        "campaigns.html",
        campaigns=campaigns,
        offer_types=OFFER_TYPES,
    )


@campaign_bp.route("/new", methods=["GET", "POST"])
def new_campaign():
    if request.method == "GET":
        return render_template(
            "campaign_form.html",
            offer_types=OFFER_TYPES,
        )

    name = request.form.get("name", "").strip()
    offer_type = request.form.get("offer_type", "").strip()
    offer_description = request.form.get("offer_description", "").strip()
    subject_template = request.form.get("subject_template", "").strip()
    body_template = request.form.get("body_template", "").strip()
    daily_limit = parse_limited_integer(
        request.form.get("daily_limit"),
        default=20,
        minimum=1,
        maximum=100,
    )

    if not name or len(name) > 200:
        flash("Názov kampane je povinný a môže mať najviac 200 znakov.", "error")
    elif offer_type not in OFFER_TYPES:
        flash("Vyber platný typ ponuky.", "error")
    elif not offer_description:
        flash("Stručne opíš, čo ponúkaš alebo hľadáš.", "error")
    elif not subject_template or len(subject_template) > 255:
        flash("Predmet je povinný a môže mať najviac 255 znakov.", "error")
    elif not body_template:
        flash("Text kampane nemôže byť prázdny.", "error")
    else:
        campaign = Campaign(
            name=name,
            offer_type=offer_type,
            offer_description=offer_description,
            subject_template=subject_template,
            body_template=body_template,
            daily_limit=daily_limit,
        )
        db.session.add(campaign)
        db.session.commit()
        flash("Kampaň bola vytvorená. Teraz do nej pridaj vyfiltrované firmy.", "success")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))

    return render_template(
        "campaign_form.html",
        offer_types=OFFER_TYPES,
        form=request.form,
    )


@campaign_bp.route("/<int:campaign_id>")
def campaign_detail(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    recipient_counts = {
        status: count
        for status, count in db.session.query(
            CampaignRecipient.status,
            func.count(CampaignRecipient.id),
        ).filter_by(campaign_id=campaign.id).group_by(CampaignRecipient.status)
    }
    today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = CampaignRecipient.query.filter(
        CampaignRecipient.campaign_id == campaign.id,
        CampaignRecipient.sent_at >= today_start,
    ).count()
    target_filters = dict(campaign.target_filters or {})
    if not target_filters:
        target_filters = {"email": "any", "contacted": "no"}
    target_filters["campaign_id"] = campaign.id
    return render_template(
        "campaign_detail.html",
        campaign=campaign,
        recipient_counts=recipient_counts,
        sent_today=sent_today,
        offer_types=OFFER_TYPES,
        recipient_outcomes=RECIPIENT_OUTCOMES,
        target_companies_url=url_for("main.companies", **target_filters),
    )


@campaign_bp.route("/<int:campaign_id>/status", methods=["POST"])
def update_campaign_status(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    status = request.form.get("status", "")
    if status not in {"draft", "active", "paused", "completed"}:
        flash("Neplatný stav kampane.", "error")
    else:
        campaign.status = status
        db.session.commit()
        flash("Stav kampane bol uložený.", "success")
    return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))


@campaign_bp.route("/<int:campaign_id>/add-filtered", methods=["POST"])
def add_filtered_recipients(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.status in CAMPAIGN_TERMINAL_STATUSES:
        flash("Do ukončenej kampane nemožno pridávať firmy.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))

    filters = company_filters_from_source(request.form)
    maximum = parse_limited_integer(
        request.form.get("max_recipients"),
        default=25,
        minimum=1,
        maximum=100,
    )

    try:
        query = filtered_companies_query(filters)
    except LocationLookupError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.companies", **filters))

    email_exists = Company.contacts.any(CompanyContact.contact_type.ilike("email"))
    existing_company_ids = {
        company_id
        for (company_id,) in db.session.query(CampaignRecipient.company_id).filter_by(
            campaign_id=campaign.id
        )
    }
    candidate_query = query.filter(email_exists)
    if existing_company_ids:
        candidate_query = candidate_query.filter(
            ~Company.id.in_(existing_company_ids)
        )
    candidates = candidate_query.order_by(
        Company.outreach_relevant.desc(),
        Company.official_name,
    ).limit(maximum * 10).all()
    added = 0
    skipped_suppressed = 0

    for company in candidates:
        if added >= maximum:
            break
        if company.id in existing_company_ids:
            continue

        contact = best_email_contact(company)
        if contact is None:
            continue
        email = normalize_email(contact.value)
        if is_suppressed(company, email):
            skipped_suppressed += 1
            continue

        db.session.add(
            CampaignRecipient(
                campaign=campaign,
                company=company,
                contact=contact,
                recipient_email=email,
                subject=render_campaign_template(campaign.subject_template, company),
                body=ensure_opt_out_footer(
                    render_campaign_template(campaign.body_template, company)
                ),
            )
        )
        existing_company_ids.add(company.id)
        added += 1

    campaign.target_filters = {
        name: value for name, value in filters.items() if value
    }
    db.session.commit()
    flash(
        f"Do kampane pribudlo {added} firiem; {skipped_suppressed} bolo na suppression zozname.",
        "success" if added else "warning",
    )
    return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))


@campaign_bp.route("/add-filtered", methods=["POST"])
def add_filtered_recipients_selected():
    campaign_id = parse_limited_integer(
        request.form.get("campaign_id"),
        default=0,
        minimum=0,
        maximum=2_147_483_647,
    )
    if not campaign_id:
        flash("Najprv vyber kampaň.", "error")
        filters = company_filters_from_source(request.form)
        return redirect(url_for("main.companies", **filters))
    return add_filtered_recipients(campaign_id)


@campaign_bp.route(
    "/<int:campaign_id>/recipients/<int:recipient_id>",
    methods=["POST"],
)
def update_recipient(campaign_id, recipient_id):
    recipient = CampaignRecipient.query.filter_by(
        id=recipient_id,
        campaign_id=campaign_id,
    ).first_or_404()
    if recipient.sent_at:
        flash("Odoslaný e-mail už nemožno prepísať.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign_id))

    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    action = request.form.get("action", "save")
    if not subject or len(subject) > 255 or not body:
        flash("Predmet aj text sú povinné; predmet môže mať najviac 255 znakov.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign_id))

    recipient.subject = subject
    recipient.body = ensure_opt_out_footer(body)
    recipient.last_error = None
    if action == "approve":
        suppression = is_suppressed(recipient.company, recipient.recipient_email)
        if suppression:
            recipient.status = "suppressed"
            recipient.last_error = "Kontakt je na suppression zozname."
            db.session.commit()
            flash("Kontakt je na suppression zozname a nebol schválený.", "error")
            return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign_id))
        recipient.status = "approved"
        recipient.approved_at = utcnow()
        flash("Príjemca bol schválený na odoslanie.", "success")
    else:
        recipient.status = "draft"
        recipient.approved_at = None
        flash("Návrh bol uložený.", "success")
    db.session.commit()
    return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign_id))


@campaign_bp.route("/<int:campaign_id>/send", methods=["POST"])
def send_approved_recipients(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.status in {"paused", *CAMPAIGN_TERMINAL_STATUSES}:
        flash("Kampaň je pozastavená alebo ukončená.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))

    requested = parse_limited_integer(
        request.form.get("batch_size"),
        default=10,
        minimum=1,
        maximum=100,
    )
    today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = CampaignRecipient.query.filter(
        CampaignRecipient.campaign_id == campaign.id,
        CampaignRecipient.sent_at >= today_start,
    ).count()
    remaining = max(0, campaign.daily_limit - sent_today)
    send_count = min(requested, remaining)
    if send_count == 0:
        flash("Denný limit kampane je už vyčerpaný.", "warning")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))

    recipients = CampaignRecipient.query.filter_by(
        campaign_id=campaign.id,
        status="approved",
    ).order_by(CampaignRecipient.approved_at, CampaignRecipient.id).limit(send_count).all()
    sent = 0
    failed = 0
    suppressed = 0

    for recipient in recipients:
        if is_suppressed(recipient.company, recipient.recipient_email):
            recipient.status = "suppressed"
            recipient.last_error = "Kontakt bol pred odoslaním nájdený na suppression zozname."
            db.session.commit()
            suppressed += 1
            continue

        recipient.status = "sending"
        recipient.last_error = None
        db.session.commit()
        sent_externally = False

        try:
            message = Message(
                subject=recipient.subject,
                recipients=[recipient.recipient_email],
                body=recipient.body,
            )
            mail.send(message)
            sent_externally = True

            lead = get_or_create_company_lead(
                recipient.company,
                recipient.recipient_email,
                campaign.offer_type,
            )
            lead.reason_to_contact = campaign.offer_description
            lead.suggested_message = recipient.body
            lead.status = "Oslovený"
            lead.last_contacted_at = naive_utcnow()
            db.session.add(
                LeadActivity(
                    lead=lead,
                    activity_type="Email odoslaný",
                    note=f"Kampaň: {campaign.name}\nPredmet: {recipient.subject}\n\n{recipient.body}",
                )
            )
            db.session.add(
                OutboundEmail(
                    lead=lead,
                    campaign_recipient=recipient,
                    message_id=message.msgId,
                    recipient=recipient.recipient_email,
                    subject=recipient.subject,
                    body=recipient.body,
                    sent_at=naive_utcnow(),
                )
            )
            recipient.status = "sent"
            recipient.sent_at = utcnow()
            campaign.status = "active"
            db.session.commit()
            sent += 1
        except Exception as exc:
            db.session.rollback()
            recipient = db.session.get(CampaignRecipient, recipient.id)
            recipient.status = "sending" if sent_externally else "failed"
            recipient.last_error = (
                "E-mail mohol byť odoslaný, ale zápis do databázy zlyhal; pred opakovaním ho skontroluj."
                if sent_externally
                else str(exc)[:1000]
            )
            db.session.commit()
            failed += 1

    if not recipients:
        flash("Kampaň nemá schválených príjemcov.", "warning")
    else:
        flash(
            f"Odoslané: {sent}, chyby alebo kontrola: {failed}, potlačené: {suppressed}.",
            "success" if sent else "warning",
        )
    return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))


@campaign_bp.route(
    "/<int:campaign_id>/recipients/<int:recipient_id>/outcome",
    methods=["POST"],
)
def update_recipient_outcome(campaign_id, recipient_id):
    recipient = CampaignRecipient.query.filter_by(
        id=recipient_id,
        campaign_id=campaign_id,
    ).first_or_404()
    outcome = request.form.get("outcome", "")
    if outcome not in RECIPIENT_OUTCOMES:
        flash("Neplatný výsledok oslovenia.", "error")
    elif not recipient.sent_at:
        flash("Výsledok možno zapísať až po odoslaní.", "error")
    else:
        recipient.status = outcome
        if outcome in {"replied", "interested", "not_interested"}:
            recipient.replied_at = recipient.replied_at or utcnow()
        if outcome in {"bounced", "opted_out"}:
            reason = (
                "Príjemca odmietol ďalšie oslovenie"
                if outcome == "opted_out"
                else "E-mail bol nedoručiteľný"
            )
            add_suppression("email", recipient.recipient_email, reason)
        db.session.commit()
        flash("Výsledok oslovenia bol uložený.", "success")
    return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign_id))


@campaign_bp.route("/suppressions", methods=["GET", "POST"])
def suppressions():
    if request.method == "POST":
        scope = request.form.get("scope", "").strip()
        value = normalize_suppression_value(scope, request.form.get("value", ""))
        reason = request.form.get("reason", "").strip()
        if scope not in {"email", "domain", "company"} or not value:
            flash("Vyber typ a zadaj hodnotu suppression pravidla.", "error")
        elif scope == "email" and value.count("@") != 1:
            flash("Zadaj platnú e-mailovú adresu.", "error")
        else:
            add_suppression(scope, value, reason)
            db.session.commit()
            flash("Suppression pravidlo bolo uložené.", "success")
            return redirect(url_for("campaigns.suppressions"))

    entries = Suppression.query.order_by(Suppression.created_at.desc()).all()
    return render_template("suppressions.html", entries=entries)


@campaign_bp.route("/suppressions/<int:suppression_id>/delete", methods=["POST"])
def delete_suppression(suppression_id):
    suppression = Suppression.query.get_or_404(suppression_id)
    db.session.delete(suppression)
    db.session.commit()
    flash("Suppression pravidlo bolo odstránené.", "success")
    return redirect(url_for("campaigns.suppressions"))
