from datetime import datetime, timedelta, timezone
import logging
from uuid import uuid4

from flask_mail import Message
from sqlalchemy import and_, func, or_, update
from sqlalchemy.orm import aliased

from extensions import db, mail
from models import (
    Campaign,
    CampaignFollowUp,
    CampaignRecipient,
    CompanyContact,
    EmailReply,
    Lead,
    LeadActivity,
    OutboundEmail,
    Suppression,
)
from services.campaigns import (
    company_suppression_value,
    email_domain,
    ensure_campaign_privacy_disclosure,
    is_suppressed,
    normalize_email,
)
from services.contact_selection import verified_contact_matches
from services.email_addresses import normalize_email_subject, normalize_valid_email
from services.crm import get_or_create_company_lead
from services.sender_profiles import send_profile_message
from services.campaign_readiness import campaign_delivery_issues
from services.website_presence import is_website_absence_eligible


def utcnow():
    return datetime.now(timezone.utc)


def naive_utcnow():
    return utcnow().replace(tzinfo=None)


DELIVERY_LOCK_TIMEOUT = timedelta(hours=1)
logger = logging.getLogger(__name__)


def sent_today_count(campaign):
    today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return CampaignRecipient.query.filter(
        CampaignRecipient.campaign_id == campaign.id,
        CampaignRecipient.sent_at >= today_start,
    ).count()


def delivery_slots_used_today(campaign):
    """Count persisted SMTP attempts, including ones with an uncertain outcome."""
    today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    initial_attempts = CampaignRecipient.query.filter(
        CampaignRecipient.campaign_id == campaign.id,
        or_(
            CampaignRecipient.sent_at >= today_start,
            CampaignRecipient.sending_started_at >= today_start,
        ),
    ).count()
    follow_up_attempts = CampaignFollowUp.query.join(CampaignRecipient).filter(
        CampaignRecipient.campaign_id == campaign.id,
        or_(CampaignFollowUp.sent_at >= today_start,
            CampaignFollowUp.sending_started_at >= today_start),
    ).count()
    return initial_attempts + follow_up_attempts


def acquire_campaign_delivery_lock(campaign_id, timeout=DELIVERY_LOCK_TIMEOUT):
    """Atomically acquire a cross-process campaign lock, recovering stale locks."""
    token = uuid4().hex
    now = naive_utcnow()
    stale_before = now - timeout
    result = db.session.execute(
        update(Campaign)
        .where(
            Campaign.id == campaign_id,
            or_(
                Campaign.delivery_lock_token.is_(None),
                Campaign.delivery_locked_at.is_(None),
                Campaign.delivery_locked_at < stale_before,
            ),
        )
        .values(delivery_lock_token=token, delivery_locked_at=now)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    return token if result.rowcount == 1 else None


def refresh_campaign_delivery_lock(campaign_id, token):
    result = db.session.execute(
        update(Campaign)
        .where(
            Campaign.id == campaign_id,
            Campaign.delivery_lock_token == token,
        )
        .values(delivery_locked_at=naive_utcnow())
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    return result.rowcount == 1


def release_campaign_delivery_lock(campaign_id, token):
    """Release only a lock still owned by this worker."""
    try:
        result = db.session.execute(
            update(Campaign)
            .where(
                Campaign.id == campaign_id,
                Campaign.delivery_lock_token == token,
            )
            .values(delivery_lock_token=None, delivery_locked_at=None)
            .execution_options(synchronize_session=False)
        )
        db.session.commit()
        return result.rowcount == 1
    except Exception:
        db.session.rollback()
        return False


def _claim_recipient(recipient_id, *, require_verified_contact=False):
    """Reserve an eligible approved row exactly once before the external call."""
    recipient = db.session.get(CampaignRecipient, recipient_id)
    if recipient is None:
        return None

    address = normalize_email(recipient.recipient_email)
    company_id = recipient.company_id
    contact_id = recipient.contact_id
    suppression_clauses = [
        and_(Suppression.scope == "email", Suppression.value == address),
        and_(
            Suppression.scope == "company",
            Suppression.value == company_suppression_value(recipient.company),
        ),
    ]
    domain = email_domain(address)
    if domain:
        suppression_clauses.append(
            and_(Suppression.scope == "domain", Suppression.value == domain)
        )
    suppressed = db.session.query(Suppression.id).filter(
        or_(*suppression_clauses)
    ).exists()
    has_reply = db.session.query(EmailReply.id).filter(
        EmailReply.campaign_recipient_id == recipient_id
    ).exists()
    conditions = [
        CampaignRecipient.id == recipient_id,
        CampaignRecipient.status == "approved",
        CampaignRecipient.replied_at.is_(None),
        CampaignRecipient.company_id == company_id,
        CampaignRecipient.recipient_email == recipient.recipient_email,
        ~has_reply,
        ~suppressed,
    ]
    cooldown_days = max(0, int(recipient.campaign.contact_cooldown_days or 0))
    if cooldown_days:
        previous_recipient = aliased(CampaignRecipient)
        cooldown_since_aware = utcnow() - timedelta(days=cooldown_days)
        cooldown_since_naive = cooldown_since_aware.replace(tzinfo=None)
        same_target = or_(
            previous_recipient.company_id == company_id,
            func.lower(func.trim(previous_recipient.recipient_email)) == address,
        )
        recent_campaign_attempt = db.session.query(previous_recipient.id).filter(
            previous_recipient.id != recipient_id,
            same_target,
            or_(
                previous_recipient.sent_at >= cooldown_since_aware,
                previous_recipient.sending_started_at >= cooldown_since_aware,
            ),
        ).exists()
        recent_legacy_outbound = db.session.query(OutboundEmail.id).join(Lead).filter(
            or_(
                Lead.company_id == company_id,
                func.lower(func.trim(OutboundEmail.recipient)) == address,
            ),
            OutboundEmail.sent_at >= cooldown_since_naive,
        ).exists()
        conditions.extend((~recent_campaign_attempt, ~recent_legacy_outbound))
    if contact_id is not None:
        valid_contact = db.session.query(CompanyContact.id).filter(
            CompanyContact.id == contact_id,
            CompanyContact.company_id == company_id,
            func.lower(func.trim(CompanyContact.contact_type)) == "email",
            func.lower(func.trim(CompanyContact.value)) == address,
        )
        if require_verified_contact:
            valid_contact = valid_contact.filter(
                CompanyContact.is_verified.is_(True)
            )
        conditions.extend(
            (CampaignRecipient.contact_id == contact_id, valid_contact.exists())
        )
    elif require_verified_contact:
        return None

    result = db.session.execute(
        update(CampaignRecipient)
        .where(*conditions)
        .values(
            status="sending",
            sending_started_at=utcnow(),
            last_error=None,
        )
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    if result.rowcount != 1:
        return None
    return db.session.get(CampaignRecipient, recipient_id)


def _empty_result(message, locked=False):
    return {
        "sent": 0,
        "failed": 0,
        "suppressed": 0,
        "unverified": 0,
        "selected": 0,
        "message": message,
        "locked": locked,
    }


def send_campaign_recipients(campaign, requested, delivery_lock_token=None):
    owns_lock = delivery_lock_token is None
    lock_token = delivery_lock_token or acquire_campaign_delivery_lock(campaign.id)
    if lock_token is None:
        return _empty_result("Kampaň práve spracúva iný proces.", locked=True)

    try:
        if not refresh_campaign_delivery_lock(campaign.id, lock_token):
            return _empty_result("Zámok kampane už vlastní iný proces.", locked=True)

        return _send_campaign_recipients_locked(campaign, requested, lock_token)
    finally:
        if owns_lock:
            release_campaign_delivery_lock(campaign.id, lock_token)


def _send_campaign_recipients_locked(campaign, requested, lock_token):
    if campaign.status in {"paused", "completed", "archived"}:
        return _empty_result("Kampaň je pozastavená alebo ukončená.")
    issues = campaign_delivery_issues(campaign)
    if issues:
        return _empty_result("Odosielanie nie je pripravené: " + " ".join(issues))

    salon_campaign = (campaign.targeting_profile or {}).get('salon_discovery') is True
    if salon_campaign:
        if not (campaign.targeting_profile or {}).get('privacy_notice_url'):
            return _empty_result('Chýba povinný odkaz na informácie o spracúvaní údajov.')
        synced_at = getattr(campaign.sender_profile, 'last_synced_at', None)
        if (synced_at is None or campaign.sender_profile.last_sync_error or
                not timedelta(0) <= naive_utcnow() - synced_at.replace(tzinfo=None) <= timedelta(minutes=5)):
            return _empty_result('Pred oslovením salónov treba úspešne skontrolovať schránku v posledných 5 minútach.')

    remaining = max(0, campaign.daily_limit - delivery_slots_used_today(campaign))
    send_count = min(max(int(requested), 0), remaining)
    if send_count == 0:
        return _empty_result("Denný limit kampane je už vyčerpaný.")

    recipients = CampaignRecipient.query.filter_by(
        campaign_id=campaign.id,
        status="approved",
    ).order_by(CampaignRecipient.approved_at, CampaignRecipient.id).limit(send_count).all()
    result = {
        "sent": 0,
        "failed": 0,
        "suppressed": 0,
        "unverified": 0,
        "selected": len(recipients),
        "message": "",
        "locked": False,
    }

    for recipient in recipients:
        if not refresh_campaign_delivery_lock(campaign.id, lock_token):
            result["message"] = "Spracovanie bolo zastavené, zámok prevzal iný proces."
            result["locked"] = True
            break

        db.session.refresh(campaign)
        if campaign.status in {"paused", "completed", "archived"} or campaign_delivery_issues(campaign):
            result["message"] = "Kampaň alebo schránka bola medzičasom pozastavená/nepripravená."
            break
        if campaign.require_no_website and not is_website_absence_eligible(recipient.company):
            recipient.status = "draft"
            recipient.approved_at = None
            recipient.last_error = "Chýba aktuálne overenie, že sa web pri kontrole nenašiel."
            db.session.commit()
            result["unverified"] += 1
            continue

        if campaign.automation_enabled or campaign.follow_up_enabled:
            if not verified_contact_matches(
                recipient.contact,
                recipient.recipient_email,
            ):
                changed = CampaignRecipient.query.filter_by(
                    id=recipient.id,
                    status="approved",
                ).update(
                    {
                        CampaignRecipient.status: "draft",
                        CampaignRecipient.approved_at: None,
                        CampaignRecipient.last_error: (
                            "Automatické odoslanie vyžaduje platný a overený "
                            "e-mailový kontakt zhodný s adresou príjemcu."
                        ),
                    },
                    synchronize_session=False,
                )
                db.session.commit()
                if changed:
                    result["unverified"] += 1
                continue

        if is_suppressed(recipient.company, recipient.recipient_email):
            changed = CampaignRecipient.query.filter_by(
                id=recipient.id,
                status="approved",
            ).update(
                {
                    CampaignRecipient.status: "suppressed",
                    CampaignRecipient.last_error: (
                        "Kontakt bol pred odoslaním nájdený na suppression zozname."
                    ),
                },
                synchronize_session=False,
            )
            db.session.commit()
            if changed:
                result["suppressed"] += 1
            continue

        if salon_campaign:
            contact = recipient.contact
            checked_at = getattr(contact, 'last_verified_at', None)
            if (contact is None or contact.source_type != 'salon_public_listing' or
                    checked_at is None or not timedelta(0) <= naive_utcnow() - checked_at.replace(tzinfo=None) <= timedelta(days=7)):
                recipient.status = 'draft'
                recipient.approved_at = None
                recipient.last_error = 'Chýba čerstvý verejný zdroj kontaktu salónu.'
                db.session.commit()
                result['unverified'] += 1
                continue
        if salon_campaign or 'privacy_notice_url' in (campaign.targeting_profile or {}):
            try:
                recipient.body = ensure_campaign_privacy_disclosure(recipient.body, campaign, recipient.contact)
            except ValueError:
                recipient.status = 'draft'
                recipient.approved_at = None
                recipient.last_error = 'Chýba platný zdroj kontaktu alebo informačná vrstva e-mailu.'
                db.session.commit()
                result['unverified'] += 1
                continue

        safe_subject = normalize_email_subject(recipient.subject)
        safe_address = normalize_valid_email(recipient.recipient_email)
        if safe_subject is None or safe_address is None:
            recipient.status = "failed"
            recipient.last_error = "Predmet alebo adresa príjemcu nie sú platné."
            db.session.commit()
            result["failed"] += 1
            continue
        recipient.subject = safe_subject
        recipient.recipient_email = safe_address

        recipient = _claim_recipient(
            recipient.id,
            require_verified_contact=(
                campaign.automation_enabled or campaign.follow_up_enabled
            ),
        )
        if recipient is None:
            continue
        sent_externally = False

        try:
            message = Message(
                subject=recipient.subject,
                recipients=[recipient.recipient_email],
                body=recipient.body,
            )
            send_profile_message(message, campaign.sender_profile)
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
            lead.next_follow_up_at = lead.last_contacted_at + timedelta(
                days=campaign.follow_up_days
            )
            db.session.add(
                LeadActivity(
                    lead=lead,
                    activity_type="Email odoslaný",
                    note=(
                        f"Kampaň: {campaign.name}\nPredmet: {recipient.subject}"
                        f"\n\n{message.body}"
                    ),
                )
            )
            outbound = OutboundEmail(
                lead=lead,
                campaign_recipient=recipient,
                message_id=message.msgId,
                recipient=recipient.recipient_email,
                subject=recipient.subject,
                body=message.body,
                sender_profile=campaign.sender_profile,
                sent_at=naive_utcnow(),
            )
            db.session.add(outbound)
            recipient.status = "sent"
            recipient.sent_at = utcnow()
            campaign.status = "active"
            db.session.flush()
            from services.campaign_followups import schedule_campaign_followup
            schedule_campaign_followup(campaign, recipient, outbound)
            db.session.commit()
            result["sent"] += 1
        except Exception:
            logger.exception("Campaign recipient delivery failed")
            db.session.rollback()
            recipient = db.session.get(CampaignRecipient, recipient.id)
            # A timeout may occur after SMTP accepted DATA. Explicit-profile
            # attempts stay ambiguous and never return to an automatic queue.
            uncertain = sent_externally or campaign.sender_profile_id is not None
            recipient.status = "sending" if uncertain else "failed"
            recipient.last_error = (
                "Výsledok odoslania nie je potvrdený. E-mail mohol byť odoslaný; "
                "pred akýmkoľvek opakovaním skontroluj schránku a históriu."
                if uncertain
                else "Odoslanie zlyhalo pred potvrdením SMTP; skontroluj nastavenie odosielania."
            )
            db.session.commit()
            result["failed"] += 1

    if not recipients:
        result["message"] = "Kampaň nemá schválených príjemcov."
    return result
