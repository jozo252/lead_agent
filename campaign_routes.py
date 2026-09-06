from datetime import datetime, timezone

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import func

from extensions import db
from models import (
    Campaign,
    CampaignRecipient,
    Company,
    CompanyContact,
    Opportunity,
    Suppression,
    SenderProfile,
    CampaignFollowUp,
)
from services.campaign_ai import CampaignAIError, generate_campaign_plan
from services.campaign_automation import (
    prepare_automated_recipients,
    run_campaign_automation,
)
from services.campaign_delivery import (send_campaign_recipients, sent_today_count,
    acquire_campaign_delivery_lock, release_campaign_delivery_lock)
from services.campaigns import (
    best_email_contact,
    clean_automated_outreach_body,
    ensure_opt_out_footer,
    ensure_validation_disclosure,
    is_suppressed,
    normalize_email,
    normalize_suppression_value,
    render_campaign_template,
)
from services.contact_selection import verified_contact_matches
from services.company_filtering import (
    company_filters_from_source,
    filtered_companies_query,
)
from services.postal_locations import LocationLookupError
from services.landing_pages import ensure_landing_page_link
from services.opportunity_scout import run_campaign_scout, validate_scout_queries
from services.opportunity_conversion import convert_verified_opportunity
from services.crm import get_or_create_company_lead
from services.hubspot import HubSpotError, sync_lead_to_hubspot
from services.campaign_readiness import campaign_delivery_issues
from services.website_presence import website_presence_summary, website_absence_filter, is_website_absence_eligible


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
OFFER_STAGES = {
    "ready": "Hotová ponuka",
    "validation": "Validačný test / pripravovaný produkt",
}
BUSINESS_LINES = {
    "general": "Všeobecné",
    "construction": "Stavebné práce",
    "electrical": "Elektro",
    "software": "Softvér",
}


def utcnow():
    return datetime.now(timezone.utc)


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
            offer_stages=OFFER_STAGES,
            business_lines=BUSINESS_LINES,
        )

    name = request.form.get("name", "").strip()
    offer_type = request.form.get("offer_type", "").strip()
    offer_description = request.form.get("offer_description", "").strip()
    subject_template = request.form.get("subject_template", "").strip()
    body_template = request.form.get("body_template", "").strip()
    offer_stage = request.form.get("offer_stage", "ready").strip()
    automation_enabled = request.form.get("automation_enabled") == "on"
    business_line = request.form.get("business_line", "general").strip()
    scout_enabled = request.form.get("scout_enabled") == "on"
    raw_scout_queries = request.form.get("scout_queries", "")
    target_hint = request.form.get("target_hint", "").strip()
    daily_limit = parse_limited_integer(
        request.form.get("daily_limit"),
        default=10,
        minimum=1,
        maximum=100,
    )
    target_total = parse_limited_integer(
        request.form.get("target_total"),
        default=50,
        minimum=1,
        maximum=1000,
    )
    batch_size = parse_limited_integer(
        request.form.get("batch_size"),
        default=10,
        minimum=1,
        maximum=100,
    )
    contact_cooldown_days = parse_limited_integer(
        request.form.get("contact_cooldown_days"),
        default=90,
        minimum=0,
        maximum=3650,
    )
    follow_up_days = parse_limited_integer(
        request.form.get("follow_up_days"),
        default=7,
        minimum=1,
        maximum=90,
    )
    targeting_profile = None

    scout_queries = [line.strip() for line in raw_scout_queries.splitlines() if line.strip()]

    if not name or len(name) > 200:
        flash("Názov kampane je povinný a môže mať najviac 200 znakov.", "error")
    elif offer_type not in OFFER_TYPES:
        flash("Vyber platný typ ponuky.", "error")
    elif offer_stage not in OFFER_STAGES:
        flash("Vyber platné štádium ponuky.", "error")
    elif business_line not in BUSINESS_LINES:
        flash("Vyber platný odbor.", "error")
    elif scout_enabled and not scout_queries:
        flash("Zapnutý lovec potrebuje aspoň jeden vyhľadávací dotaz.", "error")
    elif not offer_description:
        flash("Stručne opíš, čo ponúkaš alebo hľadáš.", "error")
    elif not automation_enabled and (not subject_template or len(subject_template) > 255):
        flash("Predmet je povinný a môže mať najviac 255 znakov.", "error")
    elif not automation_enabled and not body_template:
        flash("Text kampane nemôže byť prázdny.", "error")
    else:
        if scout_queries:
            try:
                scout_queries = validate_scout_queries(scout_queries)
            except ValueError as exc:
                flash(str(exc), "error")
                return render_template(
                    "campaign_form.html",
                    offer_types=OFFER_TYPES,
                    offer_stages=OFFER_STAGES,
                    business_lines=BUSINESS_LINES,
                    form=request.form,
                )
        if automation_enabled:
            try:
                plan = generate_campaign_plan(
                    offer_description=offer_description,
                    offer_type=offer_type,
                    offer_stage=offer_stage,
                    target_hint=target_hint,
                    campaign_name=name,
                )
            except CampaignAIError as exc:
                flash(str(exc), "error")
                return render_template(
                    "campaign_form.html",
                    offer_types=OFFER_TYPES,
                    offer_stages=OFFER_STAGES,
                    business_lines=BUSINESS_LINES,
                    form=request.form,
                )
            targeting_profile = plan["targeting_profile"]
            subject_template = subject_template or plan["subject_template"]
            body_template = body_template or plan["body_template"]
            body_template = clean_automated_outreach_body(
                body_template,
                company_name="{company_name}",
            )
            if not subject_template or not body_template:
                flash("AI nepripravila použiteľný predmet a text kampane.", "error")
                return render_template(
                    "campaign_form.html",
                    offer_types=OFFER_TYPES,
                    offer_stages=OFFER_STAGES,
                    business_lines=BUSINESS_LINES,
                    form=request.form,
                )

        if offer_stage == "validation":
            body_template = ensure_validation_disclosure(body_template)

        campaign = Campaign(
            name=name,
            offer_type=offer_type,
            offer_description=offer_description,
            subject_template=subject_template,
            body_template=body_template,
            daily_limit=daily_limit,
            automation_enabled=automation_enabled,
            business_line=business_line,
            scout_enabled=scout_enabled,
            scout_queries=scout_queries or None,
            offer_stage=offer_stage,
            targeting_profile=targeting_profile,
            target_total=target_total,
            batch_size=min(batch_size, daily_limit),
            follow_up_days=follow_up_days,
            contact_cooldown_days=contact_cooldown_days,
        )
        db.session.add(campaign)
        db.session.commit()
        flash(
            "Kampaň bola vytvorená. Skontroluj AI zacielenie a text; aktivácia "
            "automatickej kampane schváli budúce denné dávky na odoslanie."
            if automation_enabled
            else "Kampaň bola vytvorená. Teraz do nej pridaj vyfiltrované firmy.",
            "success",
        )
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))

    return render_template(
        "campaign_form.html",
        offer_types=OFFER_TYPES,
        offer_stages=OFFER_STAGES,
        business_lines=BUSINESS_LINES,
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
    sent_today = sent_today_count(campaign)
    target_filters = dict(campaign.target_filters or {})
    if not target_filters:
        target_filters = {"email": "any", "contacted": "no"}
    target_filters["campaign_id"] = campaign.id
    opportunities = Opportunity.query.filter_by(campaign_id=campaign.id).order_by(
        Opportunity.fit_score.desc(),
        Opportunity.discovered_at.desc(),
    ).limit(50).all()
    return render_template(
        "campaign_detail.html",
        campaign=campaign,
        recipient_counts=recipient_counts,
        sent_today=sent_today,
        offer_types=OFFER_TYPES,
        offer_stages=OFFER_STAGES,
        business_lines=BUSINESS_LINES,
        opportunities=opportunities,
        recipient_outcomes=RECIPIENT_OUTCOMES,
        sender_profiles=SenderProfile.query.order_by(SenderProfile.id).all(),
        workflow_issues=campaign_delivery_issues(campaign),
        website_presence_summary=website_presence_summary,
        followups=CampaignFollowUp.query.join(CampaignRecipient).filter(CampaignRecipient.campaign_id == campaign.id).order_by(CampaignFollowUp.due_at).limit(100).all(),
        target_companies_url=url_for("main.companies", **target_filters),
    )


@campaign_bp.route("/<int:campaign_id>/scout-settings", methods=["POST"])
def update_scout_settings(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    business_line = request.form.get("business_line", "general").strip()
    enabled = request.form.get("scout_enabled") == "on"
    raw_queries = request.form.get("scout_queries", "")
    queries = [line.strip() for line in raw_queries.splitlines() if line.strip()]

    if business_line not in BUSINESS_LINES:
        flash("Vyber platný odbor.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))
    if enabled and not queries:
        flash("Zapnutý lovec potrebuje aspoň jeden vyhľadávací dotaz.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))
    try:
        queries = validate_scout_queries(queries) if queries else []
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))

    campaign.business_line = business_line
    campaign.scout_enabled = enabled
    campaign.scout_queries = queries or None
    db.session.commit()
    flash("Nastavenie lovca zákaziek bolo uložené.", "success")
    return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))


@campaign_bp.route("/<int:campaign_id>/run-scout", methods=["POST"])
def run_scout_now(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    result = run_campaign_scout(campaign)
    if result.get("error"):
        flash(f"Lov zlyhal: {result['error']}", "error")
    elif result.get("skipped"):
        flash(result["skipped"], "warning")
    else:
        flash(
            "Lov dokončený: "
            f"{result['discovered']} nových a {result['refreshed']} obnovených príležitostí. "
            "Nebola odoslaná žiadna správa.",
            "success",
        )
    return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))


@campaign_bp.route(
    "/<int:campaign_id>/opportunities/<int:opportunity_id>/convert",
    methods=["POST"],
)
def convert_opportunity(campaign_id, opportunity_id):
    opportunity = Opportunity.query.filter_by(
        id=opportunity_id,
        campaign_id=campaign_id,
    ).first_or_404()
    try:
        lead, created = convert_verified_opportunity(
            opportunity,
            verification_confirmed=request.form.get("verification_confirmed") == "on",
            company_name=request.form.get("company_name", ""),
            verification_note=request.form.get("verification_note", ""),
            contact_source_url=request.form.get("contact_source_url", ""),
            email=request.form.get("email", ""),
            phone=request.form.get("phone", ""),
            website=request.form.get("website", ""),
            city=request.form.get("city", ""),
        )
        if created:
            db.session.commit()
            flash(
                "Overená príležitosť bola zmenená na CRM lead. "
                "Koncept je uložený na kontrolu; nič nebolo odoslané.",
                "success",
            )
        else:
            flash("Táto príležitosť už má CRM lead.", "warning")
        return redirect(url_for("main.lead_detail", lead_id=lead.id))
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except Exception as exc:
        db.session.rollback()
        flash(f"CRM lead sa nepodarilo vytvoriť: {exc}", "error")
    return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign_id))


@campaign_bp.route("/<int:campaign_id>/status", methods=["POST"])
def update_campaign_status(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    status = request.form.get("status", "")
    if status not in {"draft", "active", "paused", "completed"}:
        flash("Neplatný stav kampane.", "error")
    elif status == "active" and campaign_delivery_issues(campaign):
        flash("Kampaň nemožno aktivovať: " + " ".join(campaign_delivery_issues(campaign)), "error")
    elif status == "active" and campaign.automation_enabled and not campaign.targeting_profile:
        flash("Automatickej kampani chýba AI profil zacielenia.", "error")
    else:
        approved_automatic_drafts = 0
        if status == "active" and campaign.automation_enabled:
            automatic_drafts = CampaignRecipient.query.filter_by(
                campaign_id=campaign.id,
                selection_source="automation",
                status="draft",
            ).all()
            for recipient in automatic_drafts:
                if verified_contact_matches(
                    recipient.contact,
                    recipient.recipient_email,
                ):
                    recipient.status = "approved"
                    recipient.approved_at = utcnow()
                    recipient.last_error = None
                    approved_automatic_drafts += 1
                else:
                    recipient.last_error = (
                        "Automatická aktivácia vyžaduje overený e-mail. "
                        "Kontakt skontroluj a schváľ ručne."
                    )
        campaign.status = status
        if status == "active" and campaign.activated_at is None:
            campaign.activated_at = utcnow()
        if status == "completed":
            campaign.completed_at = utcnow()
        db.session.commit()
        flash(
            (
                "Kampaň je aktívna. Budúce automatické dávky sú schválené na "
                f"odoslanie; schválené vybrané firmy: {approved_automatic_drafts}."
            )
            if status == "active" and campaign.automation_enabled
            else "Stav kampane bol uložený.",
            "success",
        )
    return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))


@campaign_bp.route("/<int:campaign_id>/select-companies", methods=["POST"])
def select_automatic_companies(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not campaign.automation_enabled:
        flash("Automatický výber je dostupný iba pre AI kampaň.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))
    if campaign.status not in {"draft", "paused"}:
        flash("Firmy vyberaj iba v koncepte alebo počas pozastavenia kampane.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))
    if not campaign.targeting_profile:
        flash("Kampani chýba AI profil so SK NACE a kľúčovými slovami.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))

    requested = parse_limited_integer(
        request.form.get("selection_count"),
        default=campaign.batch_size,
        minimum=1,
        maximum=100,
    )
    reserved_count = CampaignRecipient.query.filter(
        CampaignRecipient.campaign_id == campaign.id,
        CampaignRecipient.status.notin_({"failed", "suppressed"}),
    ).count()
    remaining = max(0, campaign.target_total - reserved_count)
    requested = min(requested, remaining)
    if requested == 0:
        flash("Celkový cieľ kampane je už vybranými firmami naplnený.", "warning")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))

    try:
        result = prepare_automated_recipients(
            campaign,
            requested,
            recipient_status="draft",
        )
    except Exception as exc:
        db.session.rollback()
        flash(f"Automatický výber firiem zlyhal: {str(exc)[:300]}", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))

    if result["prepared"]:
        message = (
            f"Vybrané firmy: {result['prepared']}; bez e-mailu: "
            f"{result['without_email']}; potlačené: {result['suppressed']}. "
            "Firmy sú zatiaľ koncepty a odošlú sa až po aktivácii kampane."
        )
        category = "success"
    elif result["considered"] == 0:
        message = (
            "Podľa uloženého SK NACE, kľúčových slov a lokality sa nenašli "
            "žiadne firmy. "
            "Skontroluj AI zacielenie kampane."
        )
        category = "warning"
    elif result["qualified"] == 0:
        message = (
            f"Databáza našla {result['considered']} kandidátov, ale AI nedala "
            f"žiadnemu minimálnu zhodu {result['minimum_score']}/100. "
            "SK NACE je pravdepodobne príliš široké alebo nezodpovedá segmentu."
        )
        category = "warning"
    else:
        message = (
            f"AI kvalifikovala {result['qualified']} firiem, ale nepodarilo sa "
            f"získať použiteľný e-mail. Bez e-mailu: {result['without_email']}; "
            f"potlačené: {result['suppressed']}."
        )
        category = "warning"
    flash(message, category)
    return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))


@campaign_bp.route("/<int:campaign_id>/regenerate-targeting", methods=["POST"])
def regenerate_automatic_targeting(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not campaign.automation_enabled:
        flash("AI zacielenie možno regenerovať iba pri automatickej kampani.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))
    if campaign.status == "active":
        flash("Pred regenerovaním zacielenia kampaň pozastav.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))
    if campaign.recipients:
        flash(
            "Zacielenie nemožno regenerovať po výbere firiem. Najprv uprav "
            "SK NACE ručne alebo vytvor novú kampaň.",
            "error",
        )
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))

    profile = campaign.targeting_profile or {}
    target_hint = str(profile.get("target_hint") or campaign.name).strip()
    try:
        plan = generate_campaign_plan(
            offer_description=campaign.offer_description,
            offer_type=campaign.offer_type,
            offer_stage=campaign.offer_stage,
            target_hint=target_hint,
            campaign_name=campaign.name,
        )
    except CampaignAIError as exc:
        flash(f"Regenerovanie zacielenia zlyhalo: {exc}", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))

    campaign.targeting_profile = plan["targeting_profile"]
    campaign.last_automation_error = None
    db.session.commit()
    flash(
        "AI zacielenie bolo regenerované aj z názvu kampane. Skontroluj nové "
        "SK NACE pred výberom firiem.",
        "success",
    )
    return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))


@campaign_bp.route("/<int:campaign_id>/automation-settings", methods=["POST"])
def update_automation_settings(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not campaign.automation_enabled:
        flash("Táto kampaň nemá zapnutú automatizáciu.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))
    if campaign.status == "active":
        flash("Pred úpravou automatickú kampaň pozastav.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))

    subject_template = request.form.get("subject_template", "").strip()
    body_template = request.form.get("body_template", "").strip()
    if not subject_template or len(subject_template) > 255 or not body_template:
        flash("Predmet a text sú povinné; predmet môže mať najviac 255 znakov.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))

    def comma_values(name, maximum):
        return [
            value.strip()[:120]
            for value in request.form.get(name, "").split(",")
            if value.strip()
        ][:maximum]

    profile = dict(campaign.targeting_profile or {})
    profile["ideal_customer_profile"] = request.form.get(
        "ideal_customer_profile", ""
    ).strip()[:1000]
    profile["nace_keywords"] = comma_values("nace_keywords", 8)
    profile["company_keywords"] = comma_values("company_keywords", 10)
    profile["location_keywords"] = comma_values("location_keywords", 8)
    profile["minimum_fit_score"] = parse_limited_integer(
        request.form.get("minimum_fit_score"),
        default=60,
        minimum=0,
        maximum=100,
    )
    if not profile["nace_keywords"] and not profile["company_keywords"]:
        flash("Zadaj aspoň jedno SK NACE alebo firemné kľúčové slovo.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))

    campaign.subject_template = subject_template
    campaign.body_template = (
        ensure_validation_disclosure(body_template)
        if campaign.offer_stage == "validation"
        else body_template
    )
    campaign.targeting_profile = profile
    campaign.target_total = parse_limited_integer(
        request.form.get("target_total"),
        default=campaign.target_total,
        minimum=1,
        maximum=1000,
    )
    campaign.daily_limit = parse_limited_integer(
        request.form.get("daily_limit"),
        default=campaign.daily_limit,
        minimum=1,
        maximum=100,
    )
    campaign.batch_size = min(
        parse_limited_integer(
            request.form.get("batch_size"),
            default=campaign.batch_size,
            minimum=1,
            maximum=100,
        ),
        campaign.daily_limit,
    )
    campaign.contact_cooldown_days = parse_limited_integer(
        request.form.get("contact_cooldown_days"),
        default=campaign.contact_cooldown_days,
        minimum=0,
        maximum=3650,
    )
    campaign.follow_up_days = parse_limited_integer(
        request.form.get("follow_up_days"),
        default=campaign.follow_up_days,
        minimum=1,
        maximum=90,
    )
    db.session.commit()
    flash("Nastavenie automatickej kampane bolo uložené.", "success")
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
    if campaign.require_no_website:
        candidate_query = candidate_query.filter(website_absence_filter())
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
                    ensure_landing_page_link(
                        render_campaign_template(campaign.body_template, company),
                        campaign,
                    )
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
    token = acquire_campaign_delivery_lock(campaign_id)
    if token is None:
        flash("Kampaň práve spracúva iný proces; schválenie nebolo zmenené.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign_id))
    try:
        return _update_recipient_locked(campaign_id, recipient_id)
    finally:
        release_campaign_delivery_lock(campaign_id, token)


def _update_recipient_locked(campaign_id, recipient_id):
    recipient = CampaignRecipient.query.filter_by(
        id=recipient_id,
        campaign_id=campaign_id,
    ).first_or_404()
    if recipient.sent_at or recipient.status in {"sending", "unknown"}:
        flash("Odoslaný alebo neistý e-mail nemožno prepísať ani znovu schváliť. Najprv over odoslanú poštu.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign_id))

    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    action = request.form.get("action", "save")
    if not subject or len(subject) > 255 or not body:
        flash("Predmet aj text sú povinné; predmet môže mať najviac 255 znakov.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign_id))

    recipient.subject = subject
    if recipient.campaign.offer_stage == "validation":
        body = ensure_validation_disclosure(body)
    recipient.body = ensure_opt_out_footer(
        ensure_landing_page_link(body, recipient.campaign)
    )
    recipient.last_error = None
    if action == "approve":
        if recipient.campaign.require_no_website and not is_website_absence_eligible(recipient.company):
            db.session.rollback()
            flash("Pred schválením je potrebné aktuálne overiť web firmy.", "error")
            return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign_id))
        if (recipient.campaign.automation_enabled or recipient.campaign.follow_up_enabled) and not verified_contact_matches(
            recipient.contact,
            recipient.recipient_email,
        ):
            recipient.status = "draft"
            recipient.approved_at = None
            recipient.last_error = (
                "Automatické odoslanie vyžaduje platný a overený e-mailový "
                "kontakt zhodný s adresou príjemcu."
            )
            db.session.commit()
            flash(recipient.last_error, "error")
            return redirect(
                url_for("campaigns.campaign_detail", campaign_id=campaign_id)
            )
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
    requested = parse_limited_integer(
        request.form.get("batch_size"),
        default=10,
        minimum=1,
        maximum=100,
    )
    result = send_campaign_recipients(campaign, requested)
    flash(
        result["message"]
        or (
            f"Odoslané: {result['sent']}, chyby alebo kontrola: "
            f"{result['failed']}, potlačené: {result['suppressed']}, "
            f"neoverené: {result['unverified']}."
        ),
        "success" if result["sent"] else "warning",
    )
    return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))


@campaign_bp.route(
    "/<int:campaign_id>/recipients/<int:recipient_id>/sync-hubspot",
    methods=["POST"],
)
def sync_recipient_hubspot(campaign_id, recipient_id):
    """Vytvorí alebo aktualizuje HubSpot až po potvrdení používateľa."""
    recipient = CampaignRecipient.query.filter_by(
        id=recipient_id,
        campaign_id=campaign_id,
    ).first_or_404()
    try:
        lead = get_or_create_company_lead(
            recipient.company,
            recipient.recipient_email,
            recipient.campaign.offer_type,
        )
        db.session.flush()
        sync_lead_to_hubspot(lead)
        db.session.commit()
        flash("Firma a kontakt boli synchronizované do HubSpotu.", "success")
    except HubSpotError as exc:
        db.session.rollback()
        flash(f"HubSpot synchronizácia zlyhala: {exc}", "error")

    return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign_id))


@campaign_bp.route("/<int:campaign_id>/run-automation", methods=["POST"])
def run_automation_now(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    result = run_campaign_automation(campaign)
    if result.get("error"):
        flash(f"Automatická dávka zlyhala: {result['error']}", "error")
    elif result.get("skipped"):
        flash(result["skipped"], "warning")
    else:
        delivery = result["delivery"]
        flash(
            f"Dávka dokončená: pripravené {result['prepared']['prepared']}, "
            f"odoslané {delivery['sent']}, celkovo {result['total_sent']} z "
            f"{result['target_total']}.",
            "success" if delivery["sent"] else "warning",
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
