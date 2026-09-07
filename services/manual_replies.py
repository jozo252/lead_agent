"""Idempotent delivery of a manually approved reply.

The database claim is committed before SMTP. A sending or unknown claim is
never retried automatically because SMTP and SQLite cannot form one atomic
transaction.
"""

import re
from datetime import datetime, timezone
from email.utils import make_msgid

from flask_mail import Message
from sqlalchemy import and_, or_, update

from extensions import db
from models import EmailReply, LeadActivity, OutboundEmail, Suppression
from services.campaigns import (
    company_suppression_value,
    email_domain,
    normalize_email,
)
from services.email_addresses import normalize_email_subject, normalize_valid_email
from services.sender_profiles import (
    SenderProfileError,
    profile_readiness,
    resolve_reply_sender_profile,
    send_profile_message,
)


MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")
MAX_REPLY_BODY_LENGTH = 50_000


class ManualReplySendError(RuntimeError):
    """A safe message suitable for the authenticated CRM interface."""


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _suppression_exists(reply, address):
    clauses = [
        and_(Suppression.scope == "email", Suppression.value == address),
        and_(Suppression.scope == "domain", Suppression.value == email_domain(address)),
    ]
    if reply.lead and reply.lead.company:
        clauses.append(and_(
            Suppression.scope == "company",
            Suppression.value == company_suppression_value(reply.lead.company),
        ))
    return db.session.query(Suppression.id).filter(or_(*clauses)).exists()


def _claim_reply(reply, message_id):
    address = normalize_email(reply.from_email)
    result = db.session.execute(
        update(EmailReply)
        .where(
            EmailReply.id == reply.id,
            EmailReply.reply_sent_at.is_(None),
            EmailReply.reply_delivery_status.is_(None),
            ~_suppression_exists(reply, address),
        )
        .values(
            reply_delivery_status="sending",
            reply_message_id=message_id,
            reply_sending_started_at=_now(),
            reply_last_error=None,
        )
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    db.session.expire_all()
    stored = db.session.get(EmailReply, reply.id)
    if result.rowcount == 1:
        return stored
    if stored is not None and _suppression_exists(stored, address):
        raise ManualReplySendError(
            "Príjemca je na suppression zozname; odpoveď nebola odoslaná."
        )
    raise ManualReplySendError(
        "Táto odpoveď už bola odoslaná alebo sa jej výsledok musí najprv skontrolovať v odoslanej pošte."
    )


def send_manual_reply(reply_id, body, *, authorized=False):
    """Send one user-approved reply while making repeated submissions harmless."""
    if authorized is not True:
        raise PermissionError("Odoslanie odpovede vyžaduje potvrdenie používateľa.")

    reply = db.session.get(EmailReply, reply_id)
    if reply is None:
        raise ManualReplySendError("Odpoveď už nie je dostupná.")
    if not reply.lead:
        raise ManualReplySendError("Odpoveď nie je priradená k leadu.")

    address = normalize_valid_email(reply.from_email)
    if not address:
        raise ManualReplySendError("Odpoveď nemá platnú adresu príjemcu.")
    normalized_body = str(body or "").strip()
    if not normalized_body:
        raise ManualReplySendError("Text odpovede je prázdny.")
    if len(normalized_body) > MAX_REPLY_BODY_LENGTH:
        raise ManualReplySendError("Text odpovede je príliš dlhý.")

    if reply.reply_sent_at or reply.reply_delivery_status is not None:
        raise ManualReplySendError(
            "Táto odpoveď už bola odoslaná alebo sa jej výsledok musí najprv skontrolovať v odoslanej pošte."
        )
    if db.session.query(_suppression_exists(reply, address)).scalar():
        raise ManualReplySendError(
            "Príjemca je na suppression zozname; odpoveď nebola odoslaná."
        )

    profile = resolve_reply_sender_profile(reply)
    if profile is not None:
        issues = profile_readiness(profile)
        if issues:
            raise SenderProfileError(" ".join(issues))

    subject = normalize_email_subject(
        " ".join(str(reply.subject or "Spolupráca").splitlines()).strip()[:255]
    ) or "Spolupráca"
    if not subject.casefold().startswith("re:"):
        subject = f"Re: {subject}"[:255]
    inbound_message_id = str(
        reply.imap_message_id or reply.postmark_message_id or ""
    ).strip()
    headers = None
    if MESSAGE_ID_RE.fullmatch(inbound_message_id):
        headers = {
            "In-Reply-To": inbound_message_id,
            "References": inbound_message_id,
        }

    message_id = make_msgid()
    reply = _claim_reply(reply, message_id)
    message = Message(
        subject=subject,
        recipients=[address],
        body=normalized_body,
        extra_headers=headers,
    )
    message.msgId = message_id
    sent_externally = False
    try:
        send_profile_message(message, profile)
        sent_externally = True
        now = _now()
        reply.ai_reply_draft = message.body
        reply.reply_delivery_status = "sent"
        reply.reply_sent_at = now
        reply.reply_last_error = None
        reply.lead.status = "Odpovedané"
        reply.lead.last_contacted_at = now
        reply.lead.next_follow_up_at = None
        db.session.add(OutboundEmail(
            lead=reply.lead,
            campaign_recipient=reply.campaign_recipient,
            sender_profile=profile,
            message_id=message_id,
            recipient=address,
            subject=subject,
            body=message.body,
            sent_at=now,
        ))
        db.session.add(LeadActivity(
            lead=reply.lead,
            activity_type="Email odoslaný",
            note=f"Odpoveď na: {subject}\n\n{message.body}",
        ))
        db.session.commit()
        return reply
    except Exception:
        db.session.rollback()
        try:
            stored = db.session.get(EmailReply, reply_id)
            if stored is not None and stored.reply_delivery_status == "sending":
                stored.reply_delivery_status = "unknown"
                stored.reply_last_error = (
                    "E-mail mohol byť odoslaný, ale zápis výsledku zlyhal; "
                    "pred ďalšou akciou skontroluj odoslanú poštu."
                    if sent_externally
                    else "SMTP nepotvrdilo výsledok; pred opakovaním skontroluj odoslanú poštu."
                )
                db.session.commit()
        except Exception:
            db.session.rollback()
        raise ManualReplySendError(
            "Výsledok odoslania nie je istý. Pred ďalšou akciou skontroluj odoslanú poštu."
        ) from None
