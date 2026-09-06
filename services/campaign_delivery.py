from datetime import datetime, timedelta, timezone
from uuid import uuid4

from flask_mail import Message
from sqlalchemy import or_, update

from extensions import db, mail
from models import Campaign, CampaignRecipient, CampaignFollowUp, LeadActivity, OutboundEmail
from services.campaigns import is_suppressed
from services.contact_selection import verified_contact_matches
from services.crm import get_or_create_company_lead
from services.sender_profiles import send_profile_message
from services.campaign_readiness import campaign_delivery_issues
from services.website_presence import is_website_absence_eligible


def utcnow():
    return datetime.now(timezone.utc)


def naive_utcnow():
    return utcnow().replace(tzinfo=None)


DELIVERY_LOCK_TIMEOUT = timedelta(hours=1)


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


def _claim_recipient(recipient_id):
    """Reserve an approved row exactly once before making the external call."""
    result = db.session.execute(
        update(CampaignRecipient)
        .where(
            CampaignRecipient.id == recipient_id,
            CampaignRecipient.status == "approved",
        )
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

        recipient = _claim_recipient(recipient.id)
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
        except Exception as exc:
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
                else str(exc)[:1000]
            )
            db.session.commit()
            result["failed"] += 1

    if not recipients:
        result["message"] = "Kampaň nemá schválených príjemcov."
    return result
