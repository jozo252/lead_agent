from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import logging
import re
from uuid import uuid4

from flask_mail import Message
from sqlalchemy import and_, or_, update

from extensions import db, mail
from models import Company, LeadActivity, OutboundEmail, QuoteRequest, Suppression
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


MONEY_QUANTUM = Decimal("0.01")
ALLOWED_CURRENCIES = {"EUR", "CZK", "USD", "GBP", "CHF", "PLN", "HUF"}
logger = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _validated_price(amount, currency, unit, vat_text, validity_days):
    try:
        parsed_amount = Decimal(str(amount)).quantize(MONEY_QUANTUM, ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Cena musí byť platné číslo.") from exc
    if parsed_amount <= 0 or parsed_amount > Decimal("9999999999.99"):
        raise ValueError("Cena musí byť väčšia ako nula a v povolenom rozsahu.")

    normalized_currency = str(currency or "").strip().upper()
    if normalized_currency not in ALLOWED_CURRENCIES:
        raise ValueError("Vyber podporovanú menu.")

    normalized_unit = " ".join(str(unit or "").split())
    if not normalized_unit or len(normalized_unit) > 50:
        raise ValueError("Jednotka ceny je povinná a môže mať najviac 50 znakov.")

    normalized_vat = " ".join(str(vat_text or "").split())
    if not normalized_vat or len(normalized_vat) > 100:
        raise ValueError("Informácia o DPH je povinná a môže mať najviac 100 znakov.")

    try:
        parsed_validity = int(validity_days)
    except (TypeError, ValueError) as exc:
        raise ValueError("Platnosť ponuky musí byť celé číslo.") from exc
    if not 1 <= parsed_validity <= 90:
        raise ValueError("Platnosť ponuky musí byť od 1 do 90 dní.")

    return (
        parsed_amount,
        normalized_currency,
        normalized_unit,
        normalized_vat,
        parsed_validity,
    )


def create_quote_request(lead, email_reply):
    if email_reply.lead_id != lead.id:
        raise ValueError("Odpoveď nepatrí k tomuto leadu.")
    existing = QuoteRequest.query.filter_by(email_reply_id=email_reply.id).first()
    if existing is not None:
        return existing, False

    recipient_email = normalize_valid_email(email_reply.from_email or lead.email)
    if not recipient_email:
        raise ValueError("K odpovedi nie je dostupná platná e-mailová adresa.")

    quote_request = QuoteRequest(
        lead=lead,
        email_reply=email_reply,
        recipient_email=recipient_email,
        request_text=(email_reply.text_body or "").strip() or None,
        status="awaiting_price",
    )
    lead.status = "Cenová ponuka"
    db.session.add(quote_request)
    db.session.add(
        LeadActivity(
            lead=lead,
            activity_type="Cenová ponuka",
            note="Zákazník čaká na cenu.",
        )
    )
    db.session.commit()
    return quote_request, True


def prepare_quote(
    quote_request,
    *,
    amount,
    currency,
    unit,
    vat_text,
    terms,
    validity_days,
    sender_signature,
):
    if quote_request.status not in {"awaiting_price", "ready"}:
        raise ValueError("Túto požiadavku už nemožno naceniť.")
    amount, currency, unit, vat_text, validity_days = _validated_price(
        amount, currency, unit, vat_text, validity_days
    )
    normalized_terms = str(terms or "").strip()
    if len(normalized_terms) > 4000:
        raise ValueError("Podmienky môžu mať najviac 4000 znakov.")
    normalized_signature = " ".join(str(sender_signature or "").split())
    if not normalized_signature or len(normalized_signature) > 255:
        raise ValueError("Podpis odosielateľa je povinný a môže mať najviac 255 znakov.")

    amount_text = f"{amount:.2f}".replace(".", ",")
    company_name = " ".join(str(quote_request.lead.company_name or "").split())
    subject = normalize_email_subject(f"Cenová ponuka – {company_name}"[:255])
    if subject is None:
        raise ValueError("Predmet cenovej ponuky nie je platný.")
    body_parts = [
        "Dobrý deň,",
        "",
        "na základe vašej požiadavky posielam cenovú ponuku:",
        "",
        f"Cena: {amount_text} {currency} / {unit}",
        f"DPH: {vat_text}",
    ]
    if normalized_terms:
        body_parts.extend([f"Podmienky: {normalized_terms}"])
    body_parts.extend(
        [
            f"Platnosť ponuky: {validity_days} dní",
            "",
            "S pozdravom",
            normalized_signature,
        ]
    )

    quote_request.amount = amount
    quote_request.currency = currency
    quote_request.unit = unit
    quote_request.vat_text = vat_text
    quote_request.terms = normalized_terms or None
    quote_request.validity_days = validity_days
    quote_request.sender_signature = normalized_signature
    quote_request.subject = subject
    quote_request.body = "\n".join(body_parts)
    quote_request.status = "ready"
    quote_request.approval_token = uuid4().hex
    quote_request.approved_at = _now()
    quote_request.last_error = None
    db.session.commit()
    return quote_request


def _is_suppressed(quote_request):
    normalized_email = normalize_email(quote_request.recipient_email)
    domain = email_domain(normalized_email)
    clauses = [
        and_(Suppression.scope == "email", Suppression.value == normalized_email)
    ]
    if domain:
        clauses.append(and_(Suppression.scope == "domain", Suppression.value == domain))
    if quote_request.lead.company_id:
        company = db.session.get(Company, quote_request.lead.company_id)
        if company is not None:
            clauses.append(
                and_(
                    Suppression.scope == "company",
                    Suppression.value == company_suppression_value(company),
                )
            )
    return Suppression.query.filter(or_(*clauses)).first() is not None


def _claim_ready_quote(quote_request_id, approval_token):
    result = db.session.execute(
        update(QuoteRequest)
        .where(
            QuoteRequest.id == quote_request_id,
            QuoteRequest.status == "ready",
            QuoteRequest.approval_token == approval_token,
        )
        .values(status="sending", sending_started_at=_now(), last_error=None)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    if result.rowcount != 1:
        return None
    return db.session.get(QuoteRequest, quote_request_id)


def send_prepared_quote(quote_request_id, approval_token, *, authorized=False):
    if authorized is not True:
        raise PermissionError("Odoslanie cenovej ponuky vyžaduje výslovné oprávnenie.")

    quote_request = db.session.get(QuoteRequest, quote_request_id)
    if quote_request is None:
        raise ValueError("Požiadavka na cenu neexistuje.")
    if quote_request.status != "ready" or quote_request.approval_token != approval_token:
        raise ValueError("Ponuka už bola odoslaná alebo nemá platné schválenie.")
    subject = normalize_email_subject(quote_request.subject)
    recipient_email = normalize_valid_email(quote_request.recipient_email)
    if subject is None or recipient_email is None:
        raise ValueError("Ponuka nemá platný predmet alebo adresu príjemcu.")
    quote_request.subject = subject
    quote_request.recipient_email = recipient_email

    reply = quote_request.email_reply
    if reply is None and OutboundEmail.query.filter(
        OutboundEmail.lead_id == quote_request.lead_id,
        OutboundEmail.sender_profile_id.is_not(None),
    ).first() is not None:
        raise SenderProfileError("Chýba pôvodná odpoveď potrebná na určenie odosielacieho účtu.")
    profile = resolve_reply_sender_profile(reply)
    if profile is not None:
        issues = profile_readiness(profile)
        if issues:
            raise SenderProfileError(" ".join(issues))

    quote_request = _claim_ready_quote(quote_request_id, approval_token)
    if quote_request is None:
        raise ValueError("Ponuka už bola odoslaná alebo nemá platné schválenie.")
    if _is_suppressed(quote_request):
        quote_request.status = "suppressed"
        quote_request.last_error = "Príjemca je na suppression zozname."
        db.session.commit()
        raise ValueError(quote_request.last_error)

    sent_externally = False
    try:
        inbound_message_id = (
            (reply.imap_message_id or reply.postmark_message_id or "").strip()
            if reply is not None else ""
        )
        headers = None
        if re.fullmatch(r"<[^<>\s]+>", inbound_message_id):
            headers = {"In-Reply-To": inbound_message_id, "References": inbound_message_id}
        message = Message(
            subject=quote_request.subject,
            recipients=[quote_request.recipient_email],
            body=quote_request.body,
            extra_headers=headers,
        )
        if profile is None:
            mail.send(message)
        else:
            send_profile_message(message, profile)
        sent_externally = True

        outbound = OutboundEmail(
            lead=quote_request.lead,
            campaign_recipient=reply.campaign_recipient if reply is not None else None,
            sender_profile_id=profile.id if profile is not None else None,
            message_id=message.msgId,
            recipient=quote_request.recipient_email,
            subject=quote_request.subject,
            body=message.body,
        )
        db.session.add(outbound)
        db.session.flush()
        quote_request.outbound_email = outbound
        quote_request.status = "sent"
        quote_request.sent_at = _now()
        quote_request.lead.status = "Cenová ponuka"
        db.session.add(
            LeadActivity(
                lead=quote_request.lead,
                activity_type="Cenová ponuka",
                note=f"Cenová ponuka odoslaná.\nPredmet: {quote_request.subject}",
            )
        )
        db.session.commit()
        return quote_request
    except Exception:
        logger.exception("Quote delivery failed after its database claim")
        db.session.rollback()
        stored = db.session.get(QuoteRequest, quote_request_id)
        stored.status = "unknown"
        stored.last_error = (
            "E-mail mohol byť odoslaný, ale zápis zlyhal; pred ďalšou akciou ho skontroluj."
            if sent_externally
            else "Odoslanie nemá potvrdený výsledok; pred opakovaním skontroluj poštu."
        )
        db.session.commit()
        raise


def submit_price_and_send(quote_request, *, authorized=False, **price):
    prepared = prepare_quote(quote_request, **price)
    return send_prepared_quote(
        prepared.id,
        prepared.approval_token,
        authorized=authorized,
    )
