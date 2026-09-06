"""Isolated sender accounts and complete, read-only inbox synchronization.

Profile records contain identity only. Credentials are resolved at call time from
SENDER_PROFILE_SETTINGS or SENDER_<CONFIG_KEY>_<SETTING> environment variables.
"""

import email
import imaplib
import os
import re
import smtplib
import ssl
from datetime import timedelta, timezone
from email import policy
from email.utils import parsedate_to_datetime
from html import escape

from flask import current_app
from flask_mail import Connection, Mail

from email_checker_service import parse_inbox_message
from extensions import db, mail
from models import OutboundEmail, SenderProfile
from services.campaigns import OPT_OUT_FOOTER


NETWORK_TIMEOUT_SECONDS = 30
SETTING_NAMES = (
    "MAIL_SERVER", "MAIL_PORT", "MAIL_USE_TLS", "MAIL_USE_SSL",
    "MAIL_USERNAME", "MAIL_PASSWORD", "IMAP_SERVER", "IMAP_PORT",
    "IMAP_USERNAME", "IMAP_PASSWORD",
)
CONFIG_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")
ADDRESS_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+$")
MONTH_NAMES = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


class SenderProfileError(RuntimeError):
    """A safe-to-display configuration or transport error without credentials."""


def resolve_reply_sender_profile(reply):
    """Resolve the account that received a reply, never today's campaign setting.

    A saved inbox profile is authoritative. Older replies may inherit one unique
    historical recipient account; mixed legacy/profile history is ambiguous.
    """
    if reply is None:
        return None
    profile_id = getattr(reply, "sender_profile_id", None)
    if profile_id is not None:
        profile = db.session.get(SenderProfile, profile_id)
        if profile is None:
            raise SenderProfileError("Pôvodný profil odosielateľa už nie je dostupný.")
        return profile

    recipient_id = getattr(reply, "campaign_recipient_id", None)
    if recipient_id is not None:
        historical_ids = {
            row[0] for row in db.session.query(OutboundEmail.sender_profile_id)
            .filter(OutboundEmail.campaign_recipient_id == recipient_id).distinct().all()
        }
        if historical_ids:
            if len(historical_ids) != 1:
                raise SenderProfileError("História odpovede obsahuje viac odosielacích účtov; vyžaduje kontrolu.")
            profile_id = next(iter(historical_ids))
            if profile_id is None:
                return None
            profile = db.session.get(SenderProfile, profile_id)
            if profile is None:
                raise SenderProfileError("Pôvodný profil odosielateľa už nie je dostupný.")
            return profile

    lead_id = getattr(reply, "lead_id", None)
    if lead_id is not None and OutboundEmail.query.filter(
        OutboundEmail.lead_id == lead_id,
        OutboundEmail.sender_profile_id.is_not(None),
    ).first() is not None:
        raise SenderProfileError("Pôvodný účet odpovede nemožno spoľahlivo určiť z histórie leadu.")
    return None


def _profile_settings(profile):
    if profile is None:
        return {
            name: current_app.config.get(name, os.environ.get(name))
            for name in SETTING_NAMES
        }
    key = str(getattr(profile, "config_key", "") or "")
    if not CONFIG_KEY_RE.fullmatch(key):
        return {}
    configured = current_app.config.get("SENDER_PROFILE_SETTINGS", {})
    if isinstance(configured, dict) and key in configured:
        values = configured[key]
        return values if isinstance(values, dict) else {}
    return {name: os.environ.get(f"SENDER_{key}_{name}") for name in SETTING_NAMES}


def _boolean(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError("Neplatná logická hodnota.")


def _port(value, default):
    if isinstance(value, bool):
        raise ValueError("Neplatný port.")
    result = int(default if value is None or value == "" else value)
    if not 1 <= result <= 65535:
        raise ValueError("Neplatný port.")
    return result


def _identity_issues(profile):
    if profile is None:
        return []
    issues = []
    if not getattr(profile, "enabled", False):
        issues.append("Profil odosielateľa je vypnutý.")
    if not CONFIG_KEY_RE.fullmatch(str(getattr(profile, "config_key", "") or "")):
        issues.append("Neplatný konfiguračný kľúč profilu.")
    name = str(getattr(profile, "sender_name", "") or "").strip()
    address = str(getattr(profile, "sender_email", "") or "").strip()
    if not name or "\r" in name or "\n" in name:
        issues.append("Chýba platné meno odosielateľa.")
    if not ADDRESS_RE.fullmatch(address):
        issues.append("Chýba platný e-mail odosielateľa.")
    return issues


def _transport_issues(settings, transport):
    issues = []
    for suffix in ("SERVER", "USERNAME", "PASSWORD"):
        name = f"{transport}_{suffix}"
        value = settings.get(name)
        if not isinstance(value, str) or not value.strip():
            issues.append(f"Chýba {name}.")
        elif suffix != "PASSWORD" and ("\r" in value or "\n" in value):
            issues.append(f"Neplatné {name}.")
    try:
        _port(settings.get(f"{transport}_PORT"), 587 if transport == "MAIL" else 993)
    except (TypeError, ValueError, OverflowError):
        issues.append(f"Neplatné {transport}_PORT.")
    if transport == "MAIL":
        try:
            use_ssl = _boolean(settings.get("MAIL_USE_SSL"))
            use_tls = _boolean(settings.get("MAIL_USE_TLS"), default=not use_ssl)
            if use_ssl and use_tls:
                issues.append("MAIL_USE_TLS a MAIL_USE_SSL nesmú byť zapnuté súčasne.")
            elif not use_ssl and not use_tls:
                issues.append("SMTP vyžaduje TLS alebo SSL.")
        except ValueError:
            issues.append("Neplatné MAIL_USE_TLS alebo MAIL_USE_SSL.")
    return issues


def _mailbox_identity_issues(profile, settings, transport):
    if profile is None:
        return []
    address = str(getattr(profile, "sender_email", "") or "").strip().casefold()
    username = str(settings.get(f"{transport}_USERNAME") or "").strip().casefold()
    if address and username and username != address:
        return [f"{transport}_USERNAME musí zodpovedať e-mailu odosielateľa; aliasy nie sú podporované."]
    return []


def profile_readiness(profile, require_imap=False):
    """Return safe readiness issues; never include configuration values."""
    settings = _profile_settings(profile)
    issues = _identity_issues(profile) + _transport_issues(settings, "MAIL")
    issues.extend(_mailbox_identity_issues(profile, settings, "MAIL"))
    if require_imap:
        issues.extend(_transport_issues(settings, "IMAP"))
        issues.extend(_mailbox_identity_issues(profile, settings, "IMAP"))
    return issues


def append_profile_signature(body, signature):
    """Keep the signature before the existing opt-out footer, without repeats."""
    text = (body or "").rstrip()
    signature = (signature or "").strip()
    if not signature:
        return text
    footer = ""
    if text.endswith(OPT_OUT_FOOTER):
        text = text[:-len(OPT_OUT_FOOTER)].rstrip()
        footer = f"\n\n{OPT_OUT_FOOTER}"
    if text != signature and not text.endswith(f"\n{signature}"):
        text = f"{text}\n\n{signature}" if text else signature
    return text + footer


def _append_html_signature(body, signature):
    signature = (signature or "").strip()
    if not body or not signature:
        return body
    block = '<p data-sender-profile-signature="true">' + escape(signature).replace("\n", "<br>") + "</p>"
    if block in body:
        return body
    for footer in (f"<p>{OPT_OUT_FOOTER}</p>", f"<p>{escape(OPT_OUT_FOOTER)}</p>"):
        if footer in body:
            return body.replace(footer, block + footer, 1)
    closing_body = re.search(r"</body\s*>", body, re.IGNORECASE)
    if closing_body:
        return body[:closing_body.start()] + block + body[closing_body.start():]
    return body + block


class _TimedProfileConnection(Connection):
    def configure_host(self):
        host = None
        try:
            if self.mail.use_ssl:
                host = smtplib.SMTP_SSL(
                    self.mail.server, self.mail.port,
                    timeout=NETWORK_TIMEOUT_SECONDS, context=ssl.create_default_context(),
                )
            else:
                host = smtplib.SMTP(
                    self.mail.server, self.mail.port, timeout=NETWORK_TIMEOUT_SECONDS,
                )
            host.set_debuglevel(0)
            if self.mail.use_tls:
                host.starttls(context=ssl.create_default_context())
            host.login(self.mail.username, self.mail.password)
            return host
        except Exception:
            if host is not None:
                try:
                    host.close()
                except Exception:
                    pass
            raise


def send_profile_message(message, profile=None):
    """Send using one explicit account; None retains the existing mail sender.

    A transport exception has an uncertain delivery outcome and must not be
    retried automatically. The caller is responsible for a persisted send claim.
    """
    if profile is None:
        return mail.send(message)
    issues = profile_readiness(profile)
    if issues:
        raise SenderProfileError(" ".join(issues))
    settings = _profile_settings(profile)
    use_ssl = _boolean(settings.get("MAIL_USE_SSL"))
    settings = {
        **settings,
        "MAIL_PORT": _port(settings.get("MAIL_PORT"), 465 if use_ssl else 587),
        "MAIL_USE_SSL": use_ssl,
        "MAIL_USE_TLS": _boolean(settings.get("MAIL_USE_TLS"), default=not use_ssl),
        "MAIL_SUPPRESS_SEND": current_app.config.get("MAIL_SUPPRESS_SEND", current_app.testing),
        "MAIL_DEBUG": False,
    }
    message.sender = (profile.sender_name.strip(), profile.sender_email.strip())
    message.reply_to = profile.sender_email.strip()
    if message.extra_headers:
        message.extra_headers = {
            key: value for key, value in message.extra_headers.items()
            if key.lower() not in {"from", "sender", "reply-to"}
        }
    message.body = append_profile_signature(message.body, profile.signature)
    message.html = _append_html_signature(message.html, profile.signature)
    try:
        state = Mail().init_mail(settings)
        with _TimedProfileConnection(state) as connection:
            connection.send(message)
    except Exception:
        raise SenderProfileError(
            "SMTP odoslanie nebolo potvrdené. Pred opakovaním skontroluj odoslanú poštu."
        ) from None


def _since_search_args(since_datetime):
    if since_datetime is None:
        return ("ALL",)
    if since_datetime.tzinfo is not None:
        since_datetime = since_datetime.astimezone(timezone.utc)
    # IMAP SINCE compares the server's INTERNALDATE calendar, which may use a
    # different offset. Include the previous day so midnight replies are covered.
    since_datetime -= timedelta(days=1)
    date = f"{since_datetime.day:02d}-{MONTH_NAMES[since_datetime.month - 1]}-{since_datetime.year:04d}"
    return ("SINCE", date)


def _search_ids(connection, since_datetime, max_messages):
    status, data = connection.uid("search", None, *_since_search_args(since_datetime))
    if status != "OK" or not isinstance(data, (list, tuple)) or len(data) != 1 or not isinstance(data[0], bytes):
        raise SenderProfileError("IMAP vyhľadávanie nebolo úplne potvrdené.")
    identifiers = data[0].split()
    if any(not value.isdigit() or int(value) < 1 for value in identifiers) or len(set(identifiers)) != len(identifiers):
        raise SenderProfileError("IMAP vrátil neplatný zoznam správ.")
    if len(identifiers) > max_messages:
        raise SenderProfileError(
            "Počet správ prekračuje limit úplnej synchronizácie. Follow-up musí počkať na úplné načítanie."
        )
    return identifiers


def _message_payload(data, identifier):
    if not isinstance(data, (list, tuple)):
        raise SenderProfileError("IMAP nevrátil úplnú správu.")
    literals = [part for part in data if isinstance(part, tuple)]
    if len(literals) != 1 or len(literals[0]) != 2:
        raise SenderProfileError("IMAP nevrátil úplnú správu.")
    header, raw = literals[0]
    if not isinstance(header, bytes) or not isinstance(raw, bytes) or not raw:
        raise SenderProfileError("IMAP nevrátil úplnú správu.")
    if any(not isinstance(part, (bytes, tuple)) for part in data):
        raise SenderProfileError("IMAP nevrátil úplnú správu.")
    metadata = b" ".join([header] + [part for part in data if isinstance(part, bytes)])
    uid_match = re.search(rb"\bUID\s+(\d+)\b", metadata, re.IGNORECASE)
    size_match = re.search(rb"\{(\d+)\}$", header)
    if not uid_match or uid_match[1] != identifier or not size_match or int(size_match[1]) != len(raw):
        raise SenderProfileError("IMAP vrátil neúplnú alebo nesúvisiacu správu.")
    return raw


def _parse_profile_message(raw):
    message = email.message_from_bytes(raw, policy=policy.default)
    if any(part.defects for part in message.walk()):
        raise SenderProfileError("IMAP správu sa nepodarilo úplne spracovať.")
    result = parse_inbox_message(message)
    if not ADDRESS_RE.fullmatch(result.get("from_email", "")):
        raise SenderProfileError("IMAP správa nemá platného odosielateľa.")
    # Missing or unusable Date must remain in the result for conservative review.
    result["received_at"] = None
    try:
        received_at = parsedate_to_datetime(str(message.get("Date", "")))
        if received_at is not None:
            if received_at.tzinfo is not None:
                received_at = received_at.astimezone(timezone.utc)
            result["received_at"] = received_at.replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        pass
    return result


def fetch_profile_messages(profile, since_datetime=None, max_messages=1000):
    """Read every matching inbox message or fail; never return a partial batch.

    SINCE covers the original UTC date plus one day for mailbox timezone offsets.
    Date headers never discard fetched messages. None selects the legacy inbox.
    """
    if isinstance(max_messages, bool) or not isinstance(max_messages, int) or max_messages < 1:
        raise SenderProfileError("Limit synchronizácie musí byť kladné celé číslo.")
    settings = _profile_settings(profile)
    issues = _identity_issues(profile) + _transport_issues(settings, "IMAP")
    issues.extend(_mailbox_identity_issues(profile, settings, "IMAP"))
    if issues:
        raise SenderProfileError(" ".join(issues))
    connection = None
    try:
        connection = imaplib.IMAP4_SSL(
            settings["IMAP_SERVER"], _port(settings.get("IMAP_PORT"), 993),
            timeout=NETWORK_TIMEOUT_SECONDS, ssl_context=ssl.create_default_context(),
        )
        connection.debug = 0
        status, _ = connection.login(settings["IMAP_USERNAME"], settings["IMAP_PASSWORD"])
        if status != "OK":
            raise SenderProfileError("IMAP prihlásenie nebolo potvrdené.")
        status, _ = connection.select("inbox", readonly=True)
        if status != "OK":
            raise SenderProfileError("IMAP sa nepripojil k inboxu.")
        identifiers = _search_ids(connection, since_datetime, max_messages)
        messages = []
        for identifier in identifiers:
            status, data = connection.uid("fetch", identifier, "(BODY.PEEK[])")
            if status != "OK":
                raise SenderProfileError("IMAP nevrátil úplnú správu.")
            messages.append(_parse_profile_message(_message_payload(data, identifier)))
        return messages
    except SenderProfileError:
        raise
    except Exception:
        raise SenderProfileError(
            "IMAP synchronizácia nebola dokončená. Follow-up zostáva zablokovaný."
        ) from None
    finally:
        if connection is not None:
            try:
                connection.logout()
            except Exception:
                pass
