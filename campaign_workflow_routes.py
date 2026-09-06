"""Explicit, CSRF-protected configuration of isolated campaign identities."""
from datetime import datetime, timezone
import re

from flask import Blueprint, flash, redirect, render_template, request, url_for

from extensions import db
from models import Campaign, CampaignFollowUp, CampaignRecipient, Company, OutboundEmail, SenderProfile
from services.campaign_readiness import campaign_delivery_issues
from services.campaigns import ensure_opt_out_footer
from services.sender_profiles import profile_readiness

workflow_bp = Blueprint("workflow", __name__)
PROFILE_PRESETS = (("WEBS", "Tvorba webov"), ("ELEKTRO", "Elektro práce"), ("MG_STAV", "M&G-STAV"))


def _ensure_profile_presets():
    profiles = {}
    for key, name in PROFILE_PRESETS:
        profile = SenderProfile.query.filter_by(config_key=key).first()
        if profile is None:
            profile = SenderProfile(config_key=key, name=name, enabled=False)
            db.session.add(profile)
        profiles[key] = profile
    db.session.flush()
    return profiles


def cancel_scheduled_followups(campaign, reason):
    for row in CampaignFollowUp.query.join(CampaignRecipient).filter(
        CampaignRecipient.campaign_id == campaign.id,
        CampaignFollowUp.status == "scheduled",
    ).all():
        row.status = "cancelled"
        row.last_error = reason
        if row.original_outbound.lead:
            row.original_outbound.lead.next_follow_up_at = None


@workflow_bp.route("/sender-profiles")
def sender_profiles():
    profiles = SenderProfile.query.order_by(SenderProfile.id).all()
    return render_template("sender_profiles.html", profiles=profiles,
                           readiness={p.id: profile_readiness(p, require_imap=True) for p in profiles})


@workflow_bp.route("/sender-profiles/prepare", methods=["POST"])
def prepare_profiles():
    _ensure_profile_presets()
    db.session.commit()
    flash("Tri profily sú pripravené. Schránky nie sú zapnuté; nič sa neodoslalo.", "success")
    return redirect(url_for("workflow.sender_profiles"))


@workflow_bp.route("/campaigns/prepare-three", methods=["POST"])
def prepare_three_campaigns():
    profiles = _ensure_profile_presets()
    presets = (
        ("WEBS", "software", "Weby – malé podniky", "Tvorba jednoduchých firemných webov", True),
        ("ELEKTRO", "electrical", "Elektro práce", "Elektro práce – doplniť konkrétny rozsah a región", False),
        ("MG_STAV", "construction", "M&G-STAV – stavebné práce", "Stavebné práce – doplniť konkrétny rozsah a región", False),
    )
    added = 0
    for key, line, name, offer, no_website in presets:
        if Campaign.query.filter_by(sender_profile_id=profiles[key].id, business_line=line).first():
            continue
        db.session.add(Campaign(
            name=name, business_line=line, offer_type="service", offer_description=offer,
            subject_template="Doplniť predmet konkrétnej ponuky",
            body_template="KONCEPT NA DOPRACOVANIE: doplniť konkrétnu ponuku, región a overené údaje odosielateľa.",
            sender_profile=profiles[key], require_no_website=no_website,
            status="draft", automation_enabled=True, scout_enabled=False,
            targeting_profile={"ideal_customer_profile": "Doplniť konkrétny segment a región",
                               "nace_keywords": [], "company_keywords": [], "location_keywords": [],
                               "minimum_fit_score": 60},
            follow_up_enabled=False, daily_limit=3, batch_size=3, target_total=10,
        ))
        added += 1
    db.session.commit()
    flash(f"Pripravené neaktívne AI koncepty: {added}. Doplň schránky, ponuky a zacielenie. Odosielanie nie je aktivované, lovec aj follow-up sú vypnuté.", "success")
    return redirect(url_for("campaigns.list_campaigns"))


@workflow_bp.route("/sender-profiles/<int:profile_id>", methods=["POST"])
def save_profile(profile_id):
    profile = SenderProfile.query.get_or_404(profile_id)
    campaigns = Campaign.query.filter_by(sender_profile_id=profile.id).all()
    # Turning a profile off is always available, including for active campaigns.
    if request.form.get("disable") == "yes":
        profile.enabled = False
        db.session.commit()
        flash("Profil je vypnutý. Už rozbehnuté SMTP spojenie tým nemožno odvolať.", "success")
        return redirect(url_for("workflow.sender_profiles"))
    if any(c.status == "active" or c.delivery_lock_token for c in campaigns):
        flash("Pred úpravou profilu pozastav jeho aktívne kampane.", "error")
        return redirect(url_for("workflow.sender_profiles"))
    email = request.form.get("sender_email", "").strip().casefold()
    name = request.form.get("sender_name", "").strip()
    signature = request.form.get("signature", "").strip()
    if (email and not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", email)) or len(email) > 255 or len(name) > 100 or any(c in name for c in "\r\n") or len(signature) > 4000:
        flash("Skontroluj platnosť adresy, mena (max. 100) a podpisu (max. 4000 znakov).", "error")
        return redirect(url_for("workflow.sender_profiles"))
    if email != (profile.sender_email or "") and OutboundEmail.query.filter_by(sender_profile_id=profile.id).first():
        flash("Adresa profilu s históriou sa nemení: odpovede musia patriť pôvodnej schránke.", "error")
        return redirect(url_for("workflow.sender_profiles"))
    identity_changed = (email, name, signature) != (profile.sender_email or "", profile.sender_name or "", profile.signature or "")
    profile.sender_email, profile.sender_name, profile.signature = email or None, name or None, signature or None
    profile.enabled = request.form.get("enabled") == "on"
    issues = profile_readiness(profile, require_imap=True) if profile.enabled else []
    if issues:
        db.session.rollback()
        flash("Profil nemožno zapnúť: " + " ".join(issues), "error")
        return redirect(url_for("workflow.sender_profiles"))
    if identity_changed:
        profile.last_synced_at = None
        profile.last_sync_error = None
        for campaign in campaigns:
            campaign.follow_up_enabled = False
            campaign.follow_up_approved_at = None
            cancel_scheduled_followups(campaign, "Zmena identity alebo podpisu vyžaduje nové schválenie.")
    db.session.commit()
    flash("Profil uložený. Uloženie nespúšťa kampaň ani testovací e-mail.", "success")
    return redirect(url_for("workflow.sender_profiles"))


@workflow_bp.route("/sender-profiles/<int:profile_id>/sync", methods=["POST"])
def sync_profile(profile_id):
    from services.campaign_followups import sync_profile_inbox
    profile = SenderProfile.query.get_or_404(profile_id)
    try:
        sync_profile_inbox(profile)
        flash("Kontrola schránky dokončená. Odpovede a odhlásenia sú zapísané; nič sa neodoslalo.", "success")
    except Exception:
        db.session.rollback()
        flash("Schránku sa nepodarilo úplne skontrolovať. Follow-up zostáva zablokovaný; over nastavenie profilu.", "error")
    return redirect(url_for("workflow.sender_profiles"))


@workflow_bp.route("/campaigns/<int:campaign_id>/workflow-settings", methods=["POST"])
def save_workflow_settings(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.status not in {"draft", "paused"} or campaign.delivery_lock_token:
        flash("Tieto nastavenia upravuj iba v koncepte alebo pozastavenej kampani.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))
    for field, maximum in (("offer_description", 10000), ("subject_template", 255), ("body_template", 10000)):
        if field in request.form:
            value = request.form.get(field, "").strip()
            if not value or len(value) > maximum or (field == "subject_template" and any(c in value for c in "\r\n")):
                db.session.rollback()
                flash("Ponuka, predmet a text nesmú byť prázdne alebo prekročiť limit.", "error")
                return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))
            setattr(campaign, field, value)
    raw_profile = request.form.get("sender_profile_id", "").strip()
    profile = db.session.get(SenderProfile, int(raw_profile)) if raw_profile.isdigit() else None
    if raw_profile and profile is None:
        flash("Vyber existujúci profil odosielateľa.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))
    subject = request.form.get("follow_up_subject_template", "").strip()
    body = request.form.get("follow_up_body_template", "").strip()
    enabled = request.form.get("follow_up_enabled") == "on"
    approved = request.form.get("follow_up_approved") == "on"
    if len(subject) > 255 or any(c in subject for c in "\r\n") or len(body) > 10000 or (enabled and (not subject or not body or not approved)):
        flash("Zapnutie vyžaduje predmet, text a výslovné schválenie jedného pripomenutia.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))
    try:
        days = int(request.form.get("follow_up_days", campaign.follow_up_days))
        if not 1 <= days <= 90:
            raise ValueError
    except (TypeError, ValueError):
        flash("Odstup follow-upu musí byť 1 až 90 dní.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))
    campaign.sender_profile = profile
    campaign.sender_profile_id = profile.id if profile else None
    campaign.require_no_website = request.form.get("require_no_website") == "on"
    campaign.follow_up_enabled = enabled
    campaign.follow_up_subject_template = subject or None
    campaign.follow_up_body_template = ensure_opt_out_footer(body) if body else None
    campaign.follow_up_approved_at = datetime.now(timezone.utc).replace(tzinfo=None) if enabled and approved else None
    campaign.follow_up_days = days
    issues = campaign_delivery_issues(campaign) if enabled else []
    if issues:
        db.session.rollback()
        flash("Follow-up nemožno zapnúť: " + " ".join(issues), "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))
    cancel_scheduled_followups(campaign, "Nastavenie kampane bolo zmenené; staré pripomenutia sa neposielajú.")
    db.session.commit()
    flash("Nastavenie uložené. Staré čakajúce pripomenutia sú zrušené; platí iba pre nové oslovenia.", "success")
    return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))


@workflow_bp.route("/campaigns/<int:campaign_id>/check-websites", methods=["POST"])
def check_campaign_websites(campaign_id):
    from services.campaign_automation import website_candidate_pool
    from services.website_presence import check_company_website
    campaign = Campaign.query.get_or_404(campaign_id)
    if not campaign.require_no_website or campaign.status not in {"draft", "paused"}:
        flash("Overenie webov patrí do pozastavenej kampane s filtrom bez webu.", "error")
        return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))
    counts = {}
    try:
        for company in website_candidate_pool(campaign, limit=5):
            check = check_company_website(company)
            counts[check.status] = counts.get(check.status, 0) + 1
        db.session.commit()
        flash(f"Kontrola najviac 5 firiem: {counts or 'žiadni ďalší kandidáti'}. Nič sa neodoslalo.", "success")
    except Exception:
        db.session.rollback()
        flash("Overenie webov zlyhalo. Neoverené firmy sa nezaradia do kampane.", "error")
    return redirect(url_for("campaigns.campaign_detail", campaign_id=campaign.id))


@workflow_bp.route("/companies/<int:company_id>/check-website-presence", methods=["POST"])
def check_company_website_presence(company_id):
    from services.website_presence import check_company_website
    company = Company.query.get_or_404(company_id)
    try:
        check_company_website(company)
        db.session.commit()
        flash("Výsledok overenia webu je uložený s dátumom a podkladmi. Nič sa neodoslalo.", "success")
    except Exception:
        db.session.rollback()
        flash("Kontrola webu zlyhala. Firma sa nepovažuje za overenú bez webu.", "error")
    return redirect(url_for("main.company_detail", company_id=company.id))
