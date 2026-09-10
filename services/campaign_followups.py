"""One approved reminder, with a fresh mailbox check and durable send claim.

No network calls are made by the default dry run. A sending/unknown row is never
automatically retried: SMTP cannot provide an exactly-once transaction with SQL.
"""

from datetime import datetime, timedelta, timezone
from email.utils import make_msgid
from hashlib import sha256

from flask_mail import Message
from sqlalchemy import and_, or_, update

from extensions import db
from models import (
    Campaign, CampaignFollowUp, CampaignRecipient, CompanyContact, EmailReply, Lead, LeadActivity,
    OutboundEmail, SenderProfile, Suppression,
)
from services.campaigns import (
    company_suppression_value, email_domain, ensure_opt_out_footer, is_suppressed,
    ensure_campaign_privacy_disclosure,
    mark_campaign_recipient_replied, normalize_email, render_campaign_template,
)
from services.contact_selection import verified_contact_matches
from services.sender_profiles import (
    fetch_profile_messages, profile_readiness, send_profile_message,
)
from services.website_presence import is_website_absence_eligible


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive_utc(value):
    if value is None:
        return None
    if value.tzinfo:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _templates(campaign, company):
    subject = render_campaign_template(campaign.follow_up_subject_template, company)
    body = render_campaign_template(campaign.follow_up_body_template, company)
    return subject, ensure_opt_out_footer(body) if body else ""


def schedule_campaign_followup(campaign, recipient, outbound):
    """Stage a single snapshot in the caller's successful first-send transaction."""
    profile = campaign.sender_profile
    if not (
        campaign.follow_up_enabled and campaign.follow_up_approved_at
        and profile and profile.id == outbound.sender_profile_id
        and not profile_readiness(profile, require_imap=True)
        and int(campaign.follow_up_days or 0) >= 1
    ):
        return None
    if campaign.require_no_website and not is_website_absence_eligible(recipient.company):
        return None
    existing = CampaignFollowUp.query.filter_by(
        campaign_recipient_id=recipient.id,
    ).first()
    if existing:
        return existing
    subject, body = _templates(campaign, recipient.company)
    if not subject or not body or len(subject) > 255 or "\n" in subject or "\r" in subject:
        return None
    row = CampaignFollowUp(
        campaign_recipient=recipient,
        original_outbound=outbound,
        sender_profile=profile,
        due_at=_naive_utc(outbound.sent_at) + timedelta(days=campaign.follow_up_days),
        subject=subject,
        body=body,
        status="scheduled",
    )
    db.session.add(row)
    return row


def _matching_outbound(message, outbounds):
    sender = normalize_email(message.get("from_email"))
    received = _naive_utc(message.get("received_at"))
    thread_ids = set(message.get("thread_message_ids") or [])
    # A colleague may reply from another address in the original thread. The
    # supplied outbounds are already scoped to the account whose inbox we read.
    threaded = [outbound for outbound in outbounds if outbound.message_id in thread_ids]
    if threaded:
        if len({item.lead_id for item in threaded}) != 1:
            raise ValueError("Odpoveď odkazuje na vlákna viacerých leadov; vyžaduje kontrolu.")
        return max(threaded, key=lambda item: _naive_utc(item.sent_at))
    # A sender may compose a new message instead of using Reply. Missing dates
    # cannot prove the message predates contact, so they also stop reminders.
    matches = [item for item in outbounds if normalize_email(item.recipient) == sender
               and (received is None or received >= _naive_utc(item.sent_at))]
    if len({item.lead_id for item in matches}) > 1:
        raise ValueError("Adresa odpovede patrí viacerým leadom; vyžaduje kontrolu vlákna.")
    return max(matches, key=lambda item: _naive_utc(item.sent_at)) if matches else None


def _message_identity(message, profile_id):
    message_id = str(message.get("message_id") or "").strip()
    if message_id:
        if len(message_id) > 255:
            raise ValueError("Inbox obsahuje neplatný identifikátor správy.")
        return message_id
    # Missing Message-ID must not cause a genuine reply to be silently ignored.
    canonical = "\n".join(str(message.get(key) or "") for key in (
        "from_email", "received_at", "subject", "body",
    ))
    return f"<missing-{profile_id}-{sha256(canonical.encode('utf-8')).hexdigest()}@local.invalid>"


def _cancel_for_reply(lead_id, reason):
    rows = CampaignFollowUp.query.join(CampaignFollowUp.original_outbound).filter(
        OutboundEmail.lead_id == lead_id,
        CampaignFollowUp.status == "scheduled",
    ).all()
    for row in rows:
        row.status = "cancelled"
        row.last_error = reason
    return len(rows)


def sync_profile_inbox(profile, since_datetime=None):
    """Import a complete account-scoped inbox scan, or fail closed as a unit.

    This method commits imported replies/opt-outs before any subsequent send.
    It deliberately rescans from the original contact, not last_synced_at.
    """
    profile_id = profile.id
    try:
        errors = profile_readiness(profile, require_imap=True)
        if errors:
            raise ValueError("Profil nemá pripravený SMTP a IMAP účet.")
        outbounds = OutboundEmail.query.filter_by(sender_profile_id=profile_id).all()
        if since_datetime is None and outbounds:
            since_datetime = min(_naive_utc(item.sent_at) for item in outbounds)
        messages = fetch_profile_messages(profile, since_datetime=since_datetime)
        result = {"imported": 0, "reconciled": 0, "skipped": 0, "cancelled": 0}
        for message in messages:
            outbound = _matching_outbound(message, outbounds)
            if outbound is None:
                result["skipped"] += 1
                continue
            message_id = _message_identity(message, profile_id)
            existing = EmailReply.query.filter_by(imap_message_id=message_id).first()
            if existing:
                same_reply = (
                    existing.lead_id == outbound.lead_id
                    and existing.campaign_recipient_id in {
                        None, outbound.campaign_recipient_id,
                    }
                    and (
                        not existing.from_email
                        or normalize_email(existing.from_email)
                        == normalize_email(message.get("from_email"))
                    )
                )
                if not same_reply:
                    raise ValueError("Identifikátor správy patrí inému leadu; nutná kontrola.")
                if existing.sender_profile_id not in {None, profile_id}:
                    raise ValueError("Identifikátor správy už patrí inému profilu; nutná kontrola.")
                if existing.sender_profile_id is None:
                    existing.sender_profile_id = profile_id
                    existing.campaign_recipient = outbound.campaign_recipient
                    result["reconciled"] += 1
                else:
                    result["skipped"] += 1

                received_at = _naive_utc(existing.received_at) or _now()
                outbound.lead.status = "Odpovedal"
                outbound.lead.next_follow_up_at = None
                suppression = mark_campaign_recipient_replied(
                    outbound.campaign_recipient,
                    received_at,
                    reply_body=str(message.get("body") or existing.text_body or "")[:20000],
                    sender_email=message.get("from_email") or existing.from_email,
                )
                if suppression is not None:
                    db.session.add(suppression)
                result["cancelled"] += _cancel_for_reply(
                    outbound.lead_id,
                    "Príjemca odpovedal; ďalšie automatické správy sú zrušené.",
                )
                continue
            received_at = _naive_utc(message.get("received_at")) or _now()
            reply = EmailReply(
                lead=outbound.lead,
                campaign_recipient=outbound.campaign_recipient,
                sender_profile_id=profile_id,
                from_email=normalize_email(message.get("from_email")),
                from_name=message.get("from_name"),
                subject=str(message.get("subject") or "")[:255],
                text_body=str(message.get("body") or "")[:20000],
                imap_message_id=message_id,
                received_at=received_at,
            )
            db.session.add(reply)
            outbound.lead.status = "Odpovedal"
            outbound.lead.next_follow_up_at = None
            suppression = mark_campaign_recipient_replied(
                outbound.campaign_recipient, received_at,
                reply_body=reply.text_body, sender_email=reply.from_email,
            )
            if suppression is not None:
                db.session.add(suppression)
            result["cancelled"] += _cancel_for_reply(outbound.lead_id, "Príjemca odpovedal; ďalšie automatické správy sú zrušené.")
            db.session.add(LeadActivity(
                lead=outbound.lead, activity_type="Odpoveď",
                note=f"Profil: {profile.name}\nPredmet: {reply.subject}\n\n{reply.text_body}",
            ))
            result["imported"] += 1
        profile.last_synced_at = _now()
        profile.last_sync_error = None
        db.session.commit()
        return result
    except Exception as exc:
        db.session.rollback()
        try:
            profile = db.session.get(SenderProfile, profile_id)
            if profile:
                profile.last_sync_error = "Úplná kontrola inboxu zlyhala; automatické follow-upy sa neodoslali."
                db.session.commit()
        except Exception:
            db.session.rollback()
        raise RuntimeError("Úplná kontrola inboxu zlyhala; odosielanie je zablokované.") from exc


def _gate(row):
    """Return (outcome, safe reason), without changing data or calling networks."""
    recipient = row.campaign_recipient
    campaign = recipient.campaign
    original = row.original_outbound
    lead = original.lead
    if row.status != "scheduled":
        return "blocked", "Follow-up už bol rezervovaný alebo spracovaný."
    if campaign.status in {"completed", "archived"}:
        return "cancelled", "Kampaň je ukončená."
    if campaign.status != "active":
        return "blocked", "Kampaň nie je aktívna."
    if not campaign.follow_up_enabled or not campaign.follow_up_approved_at:
        return "cancelled", "Automatický follow-up nie je schválený a zapnutý."
    if row.sender_profile_id != campaign.sender_profile_id or row.sender_profile_id != original.sender_profile_id:
        return "cancelled", "Profil odosielateľa sa zmenil."
    if profile_readiness(row.sender_profile, require_imap=True):
        return "blocked", "Profil nemá pripravený SMTP a IMAP účet."
    if _templates(campaign, recipient.company) != (row.subject, row.body):
        return "cancelled", "Schválená šablóna sa od naplánovania zmenila."
    if is_suppressed(recipient.company, recipient.recipient_email):
        return "suppressed", "Príjemca je na suppression zozname."
    if recipient.status != "sent" or recipient.replied_at:
        return "cancelled", "Príjemca odpovedal alebo bol zmenený jeho stav."
    if not verified_contact_matches(recipient.contact, recipient.recipient_email):
        return "cancelled", "Kontakt už nie je platný a overený."
    if (campaign.targeting_profile or {}).get('salon_discovery') is True:
        if (recipient.contact.source_type != 'salon_public_listing' or
                not (campaign.targeting_profile or {}).get('privacy_notice_url')):
            return 'blocked', 'Chýba verejný zdroj alebo informačná stránka salónovej kampane.'
        try:
            ensure_campaign_privacy_disclosure(row.body, campaign, recipient.contact)
        except ValueError:
            return 'blocked', 'Zdroj kontaktu alebo informačná stránka nie sú platné.'
    if normalize_email(original.recipient) != normalize_email(recipient.recipient_email):
        return "cancelled", "Adresa príjemcu sa zmenila."
    if campaign.require_no_website and not is_website_absence_eligible(recipient.company):
        return "cancelled", "Chýba aktuálne overenie, že sa firemný web pri vyhľadávaní nenašiel."
    if lead.status != "Oslovený":
        return "cancelled", "CRM stav už nepovoľuje automatický follow-up."
    if lead.last_contacted_at and _naive_utc(lead.last_contacted_at) > _naive_utc(original.sent_at):
        return "cancelled", "Po pôvodnej správe už prebehol ďalší kontakt."
    if OutboundEmail.query.filter(
        OutboundEmail.lead_id == lead.id,
        OutboundEmail.id != original.id,
        OutboundEmail.sent_at >= original.sent_at,
    ).first():
        return "cancelled", "Po pôvodnej správe už prebehol ďalší kontakt."
    if EmailReply.query.filter(
        EmailReply.lead_id == lead.id,
        EmailReply.received_at >= original.sent_at,
    ).first():
        return "cancelled", "CRM už obsahuje odpoveď príjemcu."
    return None, ""


def _record_gate(row, outcome, reason):
    if outcome in {"cancelled", "suppressed"}:
        row.status = outcome
        row.original_outbound.lead.next_follow_up_at = None
    row.last_error = reason
    db.session.commit()


def _claim(row):
    recipient = row.campaign_recipient
    original = row.original_outbound
    if (not verified_contact_matches(recipient.contact, original.recipient)
            or normalize_email(recipient.recipient_email) != normalize_email(original.recipient)):
        return None
    profile_id = row.sender_profile_id
    campaign_id = recipient.campaign_id
    message_id = make_msgid(domain=row.sender_profile.sender_email.rsplit("@", 1)[-1])
    valid_campaign = db.session.query(Campaign.id).filter(
        Campaign.id == campaign_id, Campaign.status == "active",
        Campaign.follow_up_enabled.is_(True), Campaign.follow_up_approved_at.isnot(None),
        Campaign.sender_profile_id == profile_id,
    ).exists()
    valid_profile = db.session.query(SenderProfile.id).filter(
        SenderProfile.id == profile_id, SenderProfile.enabled.is_(True),
    ).exists()
    # Manual replies and inbound webhooks do not hold the campaign worker lock.
    # Recheck their durable state in the same SQL statement that claims the send.
    valid_recipient = db.session.query(CampaignRecipient.id).filter(
        CampaignRecipient.id == recipient.id,
        CampaignRecipient.status == "sent", CampaignRecipient.replied_at.is_(None),
        CampaignRecipient.recipient_email == recipient.recipient_email,
        CampaignRecipient.contact_id == recipient.contact_id,
    ).exists()
    valid_contact = db.session.query(CompanyContact.id).filter(
        CompanyContact.id == recipient.contact_id, CompanyContact.is_verified.is_(True),
        CompanyContact.value == recipient.contact.value,
    ).exists()
    valid_lead = db.session.query(Lead.id).filter(
        Lead.id == original.lead_id, Lead.status == "Oslovený",
        or_(Lead.last_contacted_at.is_(None), Lead.last_contacted_at <= original.sent_at),
    ).exists()
    has_reply = db.session.query(EmailReply.id).filter(
        EmailReply.lead_id == original.lead_id,
        or_(EmailReply.received_at.is_(None), EmailReply.received_at >= original.sent_at,
            EmailReply.campaign_recipient_id == recipient.id),
    ).exists()
    has_new_contact = db.session.query(OutboundEmail.id).filter(
        OutboundEmail.lead_id == original.lead_id, OutboundEmail.id != original.id,
        OutboundEmail.sent_at >= original.sent_at,
    ).exists()
    address = normalize_email(original.recipient)
    suppressed = db.session.query(Suppression.id).filter(or_(
        and_(Suppression.scope == "email", Suppression.value == address),
        and_(Suppression.scope == "domain", Suppression.value == email_domain(address)),
        and_(Suppression.scope == "company", Suppression.value == company_suppression_value(recipient.company)),
    )).exists()
    result = db.session.execute(update(CampaignFollowUp).where(
        CampaignFollowUp.id == row.id, CampaignFollowUp.status == "scheduled",
        CampaignFollowUp.due_at <= _now(), valid_campaign, valid_profile,
        valid_recipient, valid_contact, valid_lead, ~has_reply, ~has_new_contact, ~suppressed,
    ).values(status="sending", message_id=message_id, sending_started_at=_now(), last_error=None)
        .execution_options(synchronize_session=False))
    db.session.commit()
    db.session.expire_all()
    return db.session.get(CampaignFollowUp, row.id) if result.rowcount == 1 else None


def _send_claimed(row):
    row_id = row.id
    message = Message(
        subject=row.subject, recipients=[row.original_outbound.recipient], body=row.body,
        extra_headers={"In-Reply-To": row.original_outbound.message_id, "References": row.original_outbound.message_id},
    )
    message.msgId = row.message_id
    try:
        send_profile_message(message, row.sender_profile)
        now = _now()
        row.status = "sent"
        row.sent_at = now
        row.last_error = None
        lead = row.original_outbound.lead
        lead.last_contacted_at = now
        lead.next_follow_up_at = None
        db.session.add(OutboundEmail(
            lead=lead, campaign_recipient=row.campaign_recipient,
            sender_profile_id=row.sender_profile_id, message_id=row.message_id,
            recipient=row.original_outbound.recipient, subject=message.subject,
            body=message.body, sent_at=now,
        ))
        db.session.add(LeadActivity(
            lead=lead, activity_type="Follow-up odoslaný",
            note=f"Kampaň: {row.campaign_recipient.campaign.name}\nPredmet: {message.subject}\n\n{message.body}",
        ))
        db.session.commit()
        return "sent"
    except Exception:
        db.session.rollback()
        try:
            row = db.session.get(CampaignFollowUp, row_id)
            row.status = "unknown"
            row.last_error = "Výsledok SMTP alebo zápisu nie je istý. Neopakovať automaticky; skontroluj odoslanú poštu."
            db.session.commit()
        except Exception:
            # The committed sending claim survives even if the database remains
            # unavailable. Neither sending nor unknown is selected for retry.
            db.session.rollback()
        return "unknown"


def run_due_followups(dry_run=True, campaign_id=None, limit=20):
    """Preview by default; --send also requires all persisted approval gates."""
    from services.campaign_delivery import (
        acquire_campaign_delivery_lock, delivery_slots_used_today,
        refresh_campaign_delivery_lock, release_campaign_delivery_lock,
    )

    limit = min(max(int(limit), 0), 100)
    query = CampaignFollowUp.query.join(CampaignFollowUp.campaign_recipient).filter(
        CampaignFollowUp.status == "scheduled", CampaignFollowUp.due_at <= _now(),
    )
    if campaign_id is not None:
        query = query.filter(CampaignRecipient.campaign_id == campaign_id)
    ids = [row.id for row in query.order_by(CampaignFollowUp.due_at, CampaignFollowUp.id).limit(limit).all()]
    result = {"dry_run": bool(dry_run), "selected": len(ids), "eligible": 0, "sent": 0,
              "blocked": 0, "cancelled": 0, "suppressed": 0, "unknown": 0, "items": []}
    preview_slots = {}
    for row_id in ids:
        row = db.session.get(CampaignFollowUp, row_id)
        campaign = row.campaign_recipient.campaign
        outcome, reason = _gate(row)
        if dry_run:
            slots = preview_slots.setdefault(campaign.id, delivery_slots_used_today(campaign))
            if outcome is None and slots >= campaign.daily_limit:
                outcome, reason = "blocked", "Denný limit kampane je vyčerpaný."
            outcome = outcome or "eligible"
            if outcome == "eligible":
                preview_slots[campaign.id] += 1
            result[outcome] += 1
            result["items"].append({"id": row_id, "outcome": outcome, "reason": reason or "Vyžaduje čerstvú kontrolu inboxu pred odoslaním."})
            continue
        token = acquire_campaign_delivery_lock(campaign.id)
        if token is None:
            result["blocked"] += 1
            result["items"].append({"id": row_id, "outcome": "blocked", "reason": "Kampaň spracúva iný proces."})
            continue
        try:
            db.session.expire_all()
            row = db.session.get(CampaignFollowUp, row_id)
            outcome, reason = _gate(row)
            if outcome is None and delivery_slots_used_today(campaign) >= campaign.daily_limit:
                outcome, reason = "blocked", "Denný limit kampane je vyčerpaný."
            if outcome is None:
                try:
                    sync_profile_inbox(row.sender_profile, since_datetime=row.original_outbound.sent_at)
                except Exception:
                    outcome, reason = "blocked", "Úplná kontrola inboxu zlyhala."
            if outcome is None and (campaign.targeting_profile or {}).get('salon_discovery') is True:
                from services.salon_discovery import recheck_salon_contact, salon_locations
                if not recheck_salon_contact(row.campaign_recipient.contact, salon_locations(campaign.targeting_profile)):
                    outcome, reason = 'blocked', 'Aktuálny verejný kontakt a spôsob objednávania sa nepodarilo znovu overiť.'
                else:
                    db.session.commit()
            if outcome is None:
                if not refresh_campaign_delivery_lock(campaign.id, token):
                    outcome, reason = "blocked", "Zámok kampane už vlastní iný proces."
                else:
                    db.session.expire_all()
                    row = db.session.get(CampaignFollowUp, row_id)
                    outcome, reason = _gate(row)
                    if row.status == "cancelled":
                        outcome, reason = "cancelled", row.last_error or "Príjemca odpovedal."
                    if outcome is None and delivery_slots_used_today(campaign) >= campaign.daily_limit:
                        outcome, reason = "blocked", "Denný limit kampane je vyčerpaný."
            if outcome is not None:
                if row.status == "scheduled":
                    _record_gate(row, outcome, reason)
            else:
                claimed = _claim(row)
                if claimed is None:
                    outcome, reason = "blocked", "Follow-up už nie je možné rezervovať."
                else:
                    outcome = _send_claimed(claimed)
                    reason = "" if outcome == "sent" else "Neistý výsledok; automatické opakovanie je zakázané."
            result[outcome] += 1
            result["items"].append({"id": row_id, "outcome": outcome, "reason": reason})
        finally:
            release_campaign_delivery_lock(campaign.id, token)
    return result
