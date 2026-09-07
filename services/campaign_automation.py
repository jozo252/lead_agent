import unicodedata
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, cast, func, or_
from sqlalchemy.orm import selectinload

from extensions import db
from models import Campaign, CampaignFollowUp, CampaignRecipient, Company, CompanyContact, CompanyWebsiteCheck, Lead
from services.campaign_ai import rank_campaign_candidates
from services.campaign_delivery import (
    acquire_campaign_delivery_lock,
    delivery_slots_used_today,
    release_campaign_delivery_lock,
    send_campaign_recipients,
)
from services.campaigns import (
    best_email_contact,
    clean_automated_outreach_body,
    ensure_opt_out_footer,
    ensure_validation_disclosure,
    is_suppressed,
    render_campaign_template,
)
from services.rpo_sync import enrich_company_contacts
from services.landing_pages import ensure_landing_page_link
from services.email_addresses import normalize_email_subject, normalize_valid_email
from services.website_presence import (
    WEBSITE_CHECK_MAX_AGE_DAYS,
    is_website_absence_eligible,
    website_absence_filter,
)


logger = logging.getLogger(__name__)


def utcnow():
    return datetime.now(timezone.utc)


def _normalize_text(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(
        character for character in normalized if not unicodedata.combining(character)
    ).casefold()


def _compact_text(value):
    return "".join(
        character for character in _normalize_text(value) if character.isalnum()
    )


def _company_text(company):
    values = [
        company.official_name,
        company.sk_nace_code,
        company.sk_nace_name,
        company.company_type,
        company.services,
        company.markets,
        company.regions,
        company.analysis_reason,
        company.analysis_evidence,
    ]
    values.extend(activity.description for activity in company.activities)
    return _normalize_text(" ".join(str(value or "") for value in values))


def _local_fit_score(company, profile):
    full_text = _company_text(company)
    nace_text = _normalize_text(
        f"{company.sk_nace_code or ''} {company.sk_nace_name or ''}"
    )
    score = 0
    matched = []
    for term in profile.get("nace_keywords", []):
        normalized = _normalize_text(term)
        compact = _compact_text(term)
        if normalized and (
            normalized in nace_text or (compact and compact in _compact_text(nace_text))
        ):
            score += 8
            matched.append(term)
    for term in profile.get("company_keywords", []):
        normalized = _normalize_text(term)
        if normalized and normalized in full_text:
            score += 4
            matched.append(term)
    for term in profile.get("selection_signals", []):
        normalized = _normalize_text(term)
        if normalized and normalized in full_text:
            score += 2
            matched.append(term)
    for term in profile.get("exclusion_signals", []):
        normalized = _normalize_text(term)
        if normalized and normalized in full_text:
            score -= 20
    if company.outreach_relevant is True:
        score += 2
    if best_email_contact(company) is not None:
        score += 1
    return score, matched


def campaign_candidates(campaign, limit):
    return _campaign_candidates(campaign, limit)


def website_candidate_pool(campaign, limit=5):
    """Read-only bounded pool for an explicit user-triggered website check."""
    limit = max(0, min(int(limit), 10))
    if not limit:
        return []
    return _campaign_candidates(campaign, limit, website_check_pool=True)


def _campaign_candidates(campaign, limit, *, website_check_pool=False):
    profile = campaign.targeting_profile or {}
    if not profile.get("nace_keywords") and not profile.get("company_keywords"):
        raise ValueError("Kampaň nemá AI profil s použiteľnými kľúčovými slovami.")

    existing_ids = {
        company_id
        for (company_id,) in db.session.query(CampaignRecipient.company_id).filter_by(
            campaign_id=campaign.id
        )
    }
    cooldown_since_aware = utcnow() - timedelta(
        days=campaign.contact_cooldown_days
    )
    cooldown_since_naive = cooldown_since_aware.replace(tzinfo=None)
    recently_contacted_ids = {
        company_id
        for (company_id,) in db.session.query(Lead.company_id).filter(
            Lead.company_id.isnot(None),
            Lead.last_contacted_at >= cooldown_since_naive,
        )
    }
    recently_contacted_ids.update(
        company_id
        for (company_id,) in db.session.query(CampaignRecipient.company_id).filter(
            CampaignRecipient.sent_at >= cooldown_since_aware,
        )
    )
    excluded_ids = existing_ids | recently_contacted_ids

    query = Company.query.filter(Company.terminated_on.is_(None))
    pool_ordering = []
    if website_check_pool:
        current = utcnow().replace(tzinfo=None)
        query = query.outerjoin(CompanyWebsiteCheck, CompanyWebsiteCheck.company_id == Company.id)
        pool_ordering = [CompanyWebsiteCheck.checked_at.isnot(None), CompanyWebsiteCheck.checked_at]
        query = query.filter(
            ~Company.contacts.any(func.lower(func.trim(CompanyContact.contact_type)) == "website"),
            or_(
                ~Company.website_check.has(),
                Company.website_check.has(CompanyWebsiteCheck.status.in_(["unknown", "error"])),
                Company.website_check.has(CompanyWebsiteCheck.checked_at.is_(None)),
                Company.website_check.has(CompanyWebsiteCheck.checked_at < current - timedelta(days=WEBSITE_CHECK_MAX_AGE_DAYS)),
                Company.website_check.has(CompanyWebsiteCheck.checked_at > current),
            ),
        )
    elif campaign.require_no_website:
        query = query.filter(website_absence_filter())
    if excluded_ids:
        query = query.filter(~Company.id.in_(excluded_ids))
    query = query.filter(
        or_(
            Company.contacts_checked_at.is_(None),
            Company.contacts.any(),
        )
    )

    location_matches = []
    for term in profile.get("location_keywords", []):
        value = str(term or "").strip()
        if not value:
            continue
        pattern = f"%{value}%"
        location_matches.extend(
            (
                Company.municipality.ilike(pattern),
                Company.street.ilike(pattern),
            )
        )
        compact_location = _compact_text(value)
        if compact_location.isdigit():
            compact_postal_code = func.replace(Company.postal_code, " ", "")
            location_matches.append(compact_postal_code.ilike(f"%{compact_location}%"))
    if location_matches:
        query = query.filter(or_(*location_matches))

    database_matches = []
    compact_nace_code = func.replace(
        func.replace(Company.sk_nace_code, ".", ""),
        " ",
        "",
    )
    for term in profile.get("nace_keywords", []):
        normalized = _normalize_text(term)
        compact = _compact_text(term)
        if compact:
            database_matches.append(compact_nace_code.ilike(f"%{compact}%"))
        if normalized:
            database_matches.append(Company.sk_nace_name.ilike(f"%{term}%"))
    searchable_company_fields = (
        Company.official_name,
        Company.sk_nace_name,
        Company.company_type,
        cast(Company.services, String),
        cast(Company.markets, String),
        cast(Company.regions, String),
        Company.analysis_reason,
        cast(Company.analysis_evidence, String),
    )
    for term in profile.get("company_keywords", []):
        normalized = _normalize_text(term)
        if not normalized:
            continue
        database_matches.extend(field.ilike(f"%{term}%") for field in searchable_company_fields)
    if database_matches:
        query = query.filter(or_(*database_matches))

    email_exists = Company.contacts.any(CompanyContact.contact_type.ilike("email"))
    prefilter_limit = max(1000, limit * 50)
    companies = (
        query.options(
            selectinload(Company.activities),
            selectinload(Company.contacts),
            selectinload(Company.website_check),
        )
        .order_by(
            *pool_ordering,
            email_exists.desc(),
            Company.outreach_relevant.desc(),
            Company.id,
        )
        .limit(prefilter_limit)
        .all()
    )

    scored = []
    for company in companies:
        local_score, matched = _local_fit_score(company, profile)
        if local_score <= 0:
            continue
        scored.append((local_score, len(matched), company.official_name or "", company))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    if website_check_pool:
        # Rotate past recently checked ambiguous results instead of paying to
        # search the same first five companies on every explicit batch click.
        scored.sort(key=lambda item: (
            item[3].website_check is not None and item[3].website_check.checked_at is not None,
            (item[3].website_check.checked_at or datetime.min) if item[3].website_check else datetime.min,
        ))
    return [item[3] for item in scored[:limit]]


def prepare_automated_recipients(campaign, requested, recipient_status="approved"):
    if recipient_status not in {"draft", "approved"}:
        raise ValueError("Neplatný stav pripravovaných príjemcov.")
    if requested <= 0:
        return {
            "prepared": 0,
            "considered": 0,
            "ranked": 0,
            "qualified": 0,
            "minimum_score": 60,
            "without_email": 0,
            "suppressed": 0,
        }

    candidates = campaign_candidates(campaign, limit=max(30, requested * 5))
    if not candidates:
        return {
            "prepared": 0,
            "considered": 0,
            "ranked": 0,
            "qualified": 0,
            "minimum_score": int(
                (campaign.targeting_profile or {}).get("minimum_fit_score", 60)
            ),
            "without_email": 0,
            "suppressed": 0,
        }

    ranked = rank_campaign_candidates(campaign, candidates)
    by_id = {company.id: company for company in candidates}
    minimum_score = int((campaign.targeting_profile or {}).get("minimum_fit_score", 60))
    result = {
        "prepared": 0,
        "considered": len(candidates),
        "ranked": len(ranked),
        "qualified": sum(
            1 for assessment in ranked if assessment["fit_score"] >= minimum_score
        ),
        "minimum_score": minimum_score,
        "without_email": 0,
        "suppressed": 0,
    }

    for assessment in ranked:
        if result["prepared"] >= requested:
            break
        if assessment["fit_score"] < minimum_score:
            continue
        company = by_id.get(assessment["company_id"])
        if company is None:
            continue

        if campaign.require_no_website and not is_website_absence_eligible(company):
            continue

        require_verified = recipient_status == "approved"
        contact = best_email_contact(
            company,
            require_verified=require_verified,
        )
        if contact is None and company.contacts_checked_at is None:
            enrich_company_contacts(
                companies=[company],
                delay_seconds=0,
                include_existing=False,
            )
            company = db.session.get(Company, company.id)
            contact = best_email_contact(
                company,
                require_verified=require_verified,
            )
        # Contact enrichment may just have discovered an existing website.
        if campaign.require_no_website and not is_website_absence_eligible(company):
            continue
        if contact is None:
            result["without_email"] += 1
            continue

        email = normalize_valid_email(contact.value)
        if email is None:
            result["without_email"] += 1
            continue
        if is_suppressed(company, email):
            result["suppressed"] += 1
            continue

        subject = normalize_email_subject(
            assessment["subject"] or render_campaign_template(
                campaign.subject_template,
                company,
            )
        )
        if subject is None:
            continue
        body = assessment["body"] or render_campaign_template(
            campaign.body_template,
            company,
        )
        body = clean_automated_outreach_body(
            body,
            company_name=company.official_name,
        )
        if campaign.offer_stage == "validation":
            body = ensure_validation_disclosure(body)
        body = ensure_landing_page_link(body, campaign)
        db.session.add(
            CampaignRecipient(
                campaign=campaign,
                company=company,
                contact=contact,
                recipient_email=email,
                subject=subject,
                body=ensure_opt_out_footer(body),
                fit_score=assessment["fit_score"],
                fit_reason=assessment["fit_reason"],
                selection_source="automation",
                status=recipient_status,
                approved_at=utcnow() if recipient_status == "approved" else None,
            )
        )
        db.session.flush()
        result["prepared"] += 1

    db.session.commit()
    return result


def run_campaign_automation(campaign, force=False):
    campaign_id = campaign.id
    lock_token = acquire_campaign_delivery_lock(campaign_id)
    if lock_token is None:
        return {
            "campaign_id": campaign_id,
            "skipped": "Kampaň práve spracúva iný proces.",
        }

    try:
        campaign = db.session.get(Campaign, campaign_id)
        if campaign is None:
            return {
                "campaign_id": campaign_id,
                "skipped": "Kampaň už neexistuje.",
            }
        return _run_campaign_automation_locked(campaign, force, lock_token)
    finally:
        release_campaign_delivery_lock(campaign_id, lock_token)


def _has_pending_followups(campaign):
    if not campaign.follow_up_enabled:
        return False
    return db.session.query(CampaignFollowUp.id).join(
        CampaignRecipient,
        CampaignFollowUp.campaign_recipient_id == CampaignRecipient.id,
    ).filter(
        CampaignRecipient.campaign_id == campaign.id,
        CampaignFollowUp.status.in_(["scheduled", "sending", "unknown"]),
    ).first() is not None


def _run_campaign_automation_locked(campaign, force, lock_token):
    now = utcnow()
    if not campaign.automation_enabled:
        return {"campaign_id": campaign.id, "skipped": "Automatizácia nie je zapnutá."}
    if campaign.status != "active":
        return {"campaign_id": campaign.id, "skipped": "Kampaň nie je aktívna."}
    if (
        not force
        and campaign.last_automation_run_at
        and campaign.last_automation_run_at.date() == now.date()
    ):
        return {"campaign_id": campaign.id, "skipped": "Dnešná dávka už prebehla."}

    already_sent = CampaignRecipient.query.filter(
        CampaignRecipient.campaign_id == campaign.id,
        CampaignRecipient.sent_at.isnot(None),
    ).count()
    remaining_total = max(0, campaign.target_total - already_sent)
    remaining_today = max(0, campaign.daily_limit - delivery_slots_used_today(campaign))
    requested = min(campaign.batch_size, remaining_total, remaining_today)
    if requested == 0:
        if remaining_total == 0 and not _has_pending_followups(campaign):
            campaign.status = "completed"
            campaign.completed_at = now
            db.session.commit()
        return {"campaign_id": campaign.id, "skipped": "Limit kampane je vyčerpaný."}

    campaign.last_automation_run_at = now
    campaign.last_automation_error = None
    db.session.commit()

    try:
        approved_count = CampaignRecipient.query.filter_by(
            campaign_id=campaign.id,
            status="approved",
        ).count()
        preparation = prepare_automated_recipients(
            campaign,
            max(0, requested - approved_count),
        )
        delivery = send_campaign_recipients(
            campaign,
            requested,
            delivery_lock_token=lock_token,
        )
        total_sent = already_sent + delivery["sent"]
        if total_sent >= campaign.target_total and not _has_pending_followups(campaign):
            campaign.status = "completed"
            campaign.completed_at = utcnow()
        if preparation["prepared"] == 0 and delivery["sent"] == 0:
            campaign.last_automation_error = (
                "Nenašli sa nové firmy s dostatočnou zhodou a použiteľným e-mailom."
            )
        summary = {
            "campaign_id": campaign.id,
            "prepared": preparation,
            "delivery": delivery,
            "total_sent": total_sent,
            "target_total": campaign.target_total,
        }
        campaign.last_run_summary = summary
        db.session.commit()
        return summary
    except Exception:
        logger.exception("Campaign automation failed")
        db.session.rollback()
        campaign = db.session.get(Campaign, campaign.id)
        campaign.last_automation_run_at = now
        campaign.last_automation_error = "Automatizácia zlyhala; nič ďalšie sa neodoslalo."
        campaign.last_run_summary = {
            "campaign_id": campaign.id,
            "error": campaign.last_automation_error,
        }
        db.session.commit()
        return campaign.last_run_summary


def run_due_campaigns(force=False, campaign_id=None):
    query = Campaign.query.filter_by(automation_enabled=True, status="active")
    if campaign_id is not None:
        query = query.filter_by(id=campaign_id)
    return [
        run_campaign_automation(campaign, force=force)
        for campaign in query.order_by(Campaign.id).all()
    ]
