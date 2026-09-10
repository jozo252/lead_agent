"""Bounded public salon discovery. Observed contacts, never email delivery.

``is_verified`` means the exact email was observed on the identified salon's
page. A telephone/email appointment signal is dated evidence, not proof that
the business has no booking system elsewhere. The caller owns the transaction.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from types import SimpleNamespace
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from sqlalchemy import func, or_

from extensions import db
from models import CampaignRecipient, Company, CompanyContact, CompanySource, Lead, OutboundEmail
from services.campaigns import is_suppressed
from services.contact_selection import normalized_contact_email
from services.opportunity_scout import brave_web_search
from services.safe_http import safe_http_get


SOURCE_TYPE = "salon_public_listing"
MAX_FETCHES = 12
MAX_LOCATIONS = 6
MAX_STATE_URLS = 240
SALON_TYPES = {"BeautySalon", "HairSalon", "NailSalon", "DaySpa"}
BOOKING_HOSTS = ("bookio", "reservio", "fresha", "booksy", "treatwell", "sumup",
                 "reenio", "rezervio", "timify", "simplybook", "reserva", "rezervo")
DIRECTORY_HOSTS = {"facebook.com", "instagram.com", "azet.sk", "zlatestranky.sk",
                   "firmy.sk", "google.com", "finstat.sk", "oma.sk",
                   "vsetkyfirmy.sk", "salony.sk", "orlykadernictva.eu"}


def _text(value):
    return " ".join(str(value or "").split())


def _fold(value):
    return "".join(c for c in unicodedata.normalize("NFKD", _text(value))
                   if not unicodedata.combining(c)).casefold()


def salon_locations(profile, *, include_fallback=True):
    """Return bounded primary/fallback municipalities, or [] for invalid config.

    Sharing this with selection and source revalidation keeps the permitted
    geography identical before import, initial delivery and follow-up delivery.
    """
    if not isinstance(profile, dict):
        return []
    primary = profile.get("location_keywords", [])
    fallback = profile.get("salon_discovery_fallback_locations", [])
    if (not isinstance(primary, list) or not primary or not isinstance(fallback, list)
            or any(not isinstance(item, str) or not re.fullmatch(r"[\w .-]{2,60}", item)
                   or not _text(item) for item in [*primary, *fallback])):
        return []
    locations = {}
    for item in [*primary, *fallback]:
        locations.setdefault(_fold(item), _text(item))
    if len(locations) > MAX_LOCATIONS:
        return []
    if include_fallback:
        return list(locations.values())
    return list(dict.fromkeys(locations[_fold(item)] for item in primary))


def _url(value):
    try:
        parsed = urlsplit(str(value or "").strip())
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
            return None
        # Query strings never identify a different business for these sources.
        return urlunsplit((parsed.scheme, parsed.netloc.casefold(), parsed.path or "/", "", ""))
    except (ValueError, TypeError):
        return None


def _host(url):
    return (urlsplit(url).hostname or "").removeprefix("www.")


def _notino(url):
    return _host(url) == "notino.sk"


def _unsupported_directory(url):
    host = _host(url)
    return any(host == domain or host.endswith("." + domain) for domain in DIRECTORY_HOSTS)


def _source_url(value):
    url = _url(value)
    if not url or len(url) > 1500:
        return None
    host = _host(url)
    if _notino(url):
        if not re.fullmatch(r"/salony/[a-z0-9-]+/?", urlsplit(url).path):
            return None
        return "https://www.notino.sk" + urlsplit(url).path.rstrip("/") + "/"
    if _unsupported_directory(url):
        return None
    if any(part in host for part in BOOKING_HOSTS):
        return None
    return url


def _entities(soup):
    """Read only schema entities, not arbitrary app hydration/contact objects."""
    result = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            for entity in ([*graph, node] if isinstance(graph, list) else [node]):
                if not isinstance(entity, dict):
                    continue
                types = entity.get("@type", [])
                types = [types] if isinstance(types, str) else types
                if isinstance(types, list) and SALON_TYPES.intersection(types):
                    result.append(entity)
    # Repeated identical mobile/desktop metadata is acceptable, conflicting
    # salons or addresses are not.
    unique = {json.dumps(item, sort_keys=True): item for item in result}
    return list(unique.values())


def _visible_scope(soup, *, directory):
    scope = soup.find("main") or soup.find(attrs={"role": "main"})
    if scope is None:
        if directory:
            return None
        scope = soup.body or soup
    # Keep official-site footer contacts, but a directory footer is not evidence.
    for tag in scope.find_all(["script", "style", "noscript", "nav", "svg"]):
        tag.decompose()
    if directory:
        for tag in scope.find_all(["footer", "header"]):
            tag.decompose()
    return scope


def _booking_evidence(scope, source_url, *, directory):
    text = _text(scope.get_text(" ", strip=True))
    folded = _fold(text)
    for tag in scope.find_all(["a", "iframe", "script", "form"]):
        href = str(tag.get("href") or tag.get("src") or tag.get("action") or "")
        absolute = urljoin(source_url, href)
        if any(part in _host(absolute) for part in BOOKING_HOSTS):
            return None
        label = _fold(tag.get_text(" ", strip=True))
        is_booking = re.search(r"\b(?:rezerv|book|objedna|termin)", _fold(urlsplit(href).path) + " " + label)
        if is_booking and href and not href.startswith(("tel:", "mailto:", "#")):
            return None
    if scope.find("input", attrs={"type": re.compile(r"^(date|time|datetime-local)$", re.I)}):
        return None
    if directory:
        phrase = "Tieto služby nemôžete rezervovať online"
        if _fold(phrase) not in folded or "rezervovat telefonicky" not in folded:
            return None
        remainder = folded.replace(_fold(phrase), "")
        if re.search(r"rezerv(?:ovat|acia) online|online rezerv|volne terminy", remainder):
            return None
        return {"booking_signal": "telephone_only_on_listing", "quote": phrase}
    # Appointment requests sent through a mail client are not a live calendar.
    email_phrase = "Odoslaním sa otvorí váš e-mailový klient"
    if _fold(email_phrase) in folded:
        return {"booking_signal": "email_appointment_request", "quote": email_phrase}
    if re.search(r"online rezerv|rezerv(?:ovat|acia) online|volne terminy", folded):
        return None
    # Require an explicit appointment context, not a generic telephone number.
    for match in re.finditer(r"objedn[a-z]*", folded):
        nearby = folded[max(0, match.start() - 100):match.end() + 200]
        if "telefonicky" in nearby or "emailom" in nearby:
            return {"booking_signal": "telephone_or_email_appointment_request",
                    "quote": text[max(0, match.start() - 100):match.end() + 200]}
    return None


def parse_salon_listing(html, source_url, location_keywords):
    """Return a single unambiguous public business contact, or ``None``.

    Supports Notino individual listings and structured official salon websites.
    Search snippets, platform emails and mere absence of booking are insufficient.
    """
    url = _source_url(source_url)
    if not url or not isinstance(html, str) or len(html) > 4_000_000:
        return None
    soup = BeautifulSoup(html, "html.parser")
    entities = _entities(soup)
    if len(entities) != 1:
        return None
    entity = entities[0]
    name = _text(entity.get("name"))
    if not name or len(name) > 200 or "\ufffd" in name:
        return None
    address = entity.get("address")
    if not isinstance(address, dict):
        return None
    country = address.get("addressCountry", "")
    if isinstance(country, dict):
        country = country.get("name", "")
    if _fold(country) not in {"sk", "slovensko", "slovakia", "slovenska republika"}:
        return None
    locations = {_fold(item): _text(item) for item in location_keywords if _text(item)}
    municipality = _text(address.get("addressLocality"))
    street = _text(address.get("streetAddress"))
    postal_code = _text(address.get("postalCode"))
    if not municipality:
        # Some real sites put the complete postal address into streetAddress.
        match = re.fullmatch(r"(.+?),\s*(\d{3}\s?\d{2})\s+(.+)", street)
        if not match:
            return None
        street, postal_code, municipality = map(_text, match.groups())
    if _fold(municipality) not in locations or not street:
        return None
    email = normalized_contact_email(SimpleNamespace(value=entity.get("email")))
    if not email or "notino" in email.rsplit("@", 1)[1].casefold():
        return None
    email = email.casefold()
    directory = _notino(url)
    entity_url = _url(entity.get("url"))
    if directory:
        if not entity_url or _source_url(entity_url) != url:
            return None
    elif not entity_url or _host(entity_url) != _host(url):
        return None
    # Reject embedded booking engines before removing scripts from visible HTML.
    for tag in soup.find_all(["script", "iframe"], src=True):
        if any(part in _host(urljoin(url, tag["src"])) for part in BOOKING_HOSTS):
            return None
    if any(part in str(entity.get("potentialAction", "")).casefold()
           for part in ("reserveaction", "booking", "reservio", "bookio")):
        return None
    title = _fold(soup.title.get_text(" ", strip=True) if soup.title else "")
    headings = [_fold(tag.get_text(" ", strip=True)) for tag in soup.find_all(["h1", "h2"])]
    if not any(_fold(name) in item for item in [title, *headings]):
        return None
    scope = _visible_scope(soup, directory=directory)
    if scope is None:
        return None
    text = _text(scope.get_text(" ", strip=True))
    if _fold(municipality) not in _fold(text):
        return None
    observed_emails = set()
    for tag in scope.find_all("a", href=True):
        if tag["href"].casefold().startswith("mailto:"):
            value = unquote(tag["href"][7:].split("?", 1)[0])
            normalized = normalized_contact_email(SimpleNamespace(value=value))
            if normalized:
                observed_emails.add(normalized.casefold())
    # Plain visible emails count too; collect all, so ambiguity causes rejection.
    for value in re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text):
        normalized = normalized_contact_email(SimpleNamespace(value=value))
        if normalized:
            observed_emails.add(normalized.casefold())
    if observed_emails != {email}:
        return None
    evidence = _booking_evidence(scope, url, directory=directory)
    if not evidence:
        return None
    return {
        "name": name, "municipality": locations[_fold(municipality)], "street": street,
        "postal_code": postal_code or None, "email": email, "source_url": url,
        "website_url": None if directory else entity_url,
        "name_kind": "public_business_display_name", **evidence,
    }


def recheck_salon_contact(contact, locations, *, fetch=None):
    """Refresh one observed salon contact without sending or committing.

    Failure leaves stored verification/evidence untouched, including transient
    HTTP failures. The caller must treat ``False`` as a hold for this delivery.
    Only the same public source, business identity, locality and email qualify.
    """
    company = getattr(contact, "company", None)
    source_url = _source_url(getattr(contact, "source_url", None))
    if (company is None or not source_url or contact.source_type != SOURCE_TYPE
            or _fold(contact.contact_type) != "email" or not contact.is_verified
            or not isinstance(locations, (list, tuple)) or not locations):
        return False
    email = normalized_contact_email(contact)
    if not email:
        return False
    fetch = fetch or safe_http_get
    try:
        response = fetch(source_url, timeout=20, max_bytes=4_000_000)
        final_url = _source_url(response.url)
        # Ordinary slash/canonical-www redirects are fine; a different source
        # path or host must be reviewed instead of silently refreshing evidence.
        if (response.status_code != 200 or not final_url
                or _host(final_url) != _host(source_url)
                or urlsplit(final_url).path.rstrip("/") != urlsplit(source_url).path.rstrip("/")):
            return False
        candidate = parse_salon_listing(response.text, final_url, locations)
    except Exception:
        return False
    if (candidate is None or candidate["email"] != email.casefold()
            or _fold(candidate["name"]) != _fold(company.official_name)
            or _fold(candidate["municipality"]) != _fold(company.municipality)):
        return False
    now = datetime.now(timezone.utc)
    contact.last_verified_at = now.replace(tzinfo=None)
    evidence = {**candidate, "source_url": source_url, "observed_url": final_url,
                "source_type": SOURCE_TYPE, "checked_at": now.isoformat(),
                "limitation": "Observed appointment method on this page; not global booking absence."}
    previous = company.analysis_evidence if isinstance(company.analysis_evidence, list) else []
    company.analysis_evidence = [
        item for item in previous
        if not (isinstance(item, dict) and item.get("source_type") == SOURCE_TYPE
                and _source_url(item.get("source_url")) == source_url)
    ] + [evidence]
    return True


def _duplicate_or_suppressed(candidate):
    address, url = candidate["email"], candidate["source_url"]
    if CompanyContact.query.filter(or_(
        func.lower(func.trim(CompanyContact.value)) == address,
        CompanyContact.source_url == url,
    )).first() or CompanySource.query.filter(CompanySource.resource_url == url).first():
        return "existing_contact"
    if Lead.query.filter(func.lower(func.trim(Lead.email)) == address).first():
        return "existing_lead"
    if OutboundEmail.query.filter(func.lower(func.trim(OutboundEmail.recipient)) == address).first():
        return "already_contacted"
    # Existing company identities must be reconciled, not duplicated under a new
    # record that would evade company-level suppression.
    existing = Company.query.filter(
        func.lower(func.trim(Company.official_name)) == candidate["name"].casefold(),
        func.lower(func.trim(Company.municipality)) == candidate["municipality"].casefold(),
    ).first()
    if existing:
        return "existing_company"
    if is_suppressed(SimpleNamespace(ico=None, id=None), address):
        return "suppressed"
    return None


def _uncontacted_salon_contact(contacts):
    """Select one verified imported email with no suppression or contact history."""
    emails = [item for item in contacts if _fold(item.contact_type) == "email"]
    if len(emails) != 1:
        return None
    contact = emails[0]
    if contact.source_type != SOURCE_TYPE or not contact.is_verified:
        return None
    address = normalized_contact_email(contact)
    if not address or is_suppressed(contact.company, address):
        return None
    address = address.casefold()
    matching_leads = db.session.query(Lead.id).filter(Lead.company_id == contact.company_id)
    if OutboundEmail.query.filter(or_(
        func.lower(func.trim(OutboundEmail.recipient)) == address,
        OutboundEmail.lead_id.in_(matching_leads),
    )).first():
        return None
    if Lead.query.filter(
        or_(Lead.company_id == contact.company_id, func.lower(func.trim(Lead.email)) == address),
        Lead.last_contacted_at.isnot(None),
    ).first():
        return None
    if CampaignRecipient.query.filter(
        or_(CampaignRecipient.company_id == contact.company_id,
            func.lower(func.trim(CampaignRecipient.recipient_email)) == address),
        or_(CampaignRecipient.sent_at.isnot(None), CampaignRecipient.replied_at.isnot(None),
            CampaignRecipient.sending_started_at.isnot(None),
            CampaignRecipient.status.in_(["sent", "sending", "replied", "unsubscribed"])),
    ).first():
        return None
    return contact


def _stale_uncontacted_contact(contacts, now):
    """Select one stale imported email; do not refresh manual or contacted data."""
    contact = _uncontacted_salon_contact(contacts)
    if contact is None:
        return None
    checked_at = contact.last_verified_at
    if checked_at is not None:
        checked_at = checked_at.replace(tzinfo=timezone.utc) if checked_at.tzinfo is None else checked_at
        if checked_at >= now - timedelta(days=6):
            return None
    return contact


def _has_usable_local_queue(campaign, locations, now):
    """A fresh, uncontacted local queue takes priority over wider discovery."""
    contacts = CompanyContact.query.join(Company).filter(
        CompanyContact.source_type == SOURCE_TYPE,
        CompanyContact.contact_type == "email", CompanyContact.is_verified.is_(True),
        Company.municipality.in_(locations), Company.terminated_on.is_(None),
        CompanyContact.last_verified_at >= (now - timedelta(days=7)).replace(tzinfo=None),
        CompanyContact.last_verified_at <= now.replace(tzinfo=None),
    ).order_by(CompanyContact.id).limit(120).all()
    for contact in contacts:
        if not _source_url(contact.source_url) or _uncontacted_salon_contact(contact.company.contacts) is None:
            continue
        recipient = CampaignRecipient.query.filter_by(
            campaign_id=campaign.id, company_id=contact.company_id,
        ).first()
        if recipient is None or recipient.status == "approved":
            return True
    return False


def _contact_snapshot(contact):
    """A recheck may replace only snapshot fields during a dry-run."""
    return SimpleNamespace(
        company=SimpleNamespace(
            official_name=contact.company.official_name,
            municipality=contact.company.municipality,
            # Recheck builds a new list without changing any nested evidence.
            analysis_evidence=list(contact.company.analysis_evidence or []),
        ),
        source_type=contact.source_type, contact_type=contact.contact_type,
        source_url=contact.source_url, value=contact.value,
        is_verified=contact.is_verified, last_verified_at=contact.last_verified_at,
    )


def discover_salon_contacts(campaign, *, dry_run=True, search=None, fetch=None, limit=12):
    """Search primary towns first, then the configured fallback if none qualify.

    There are at most six municipalities and twelve fetches per tier (24 only
    when fallback is needed). Fresh local queued contacts or newly discovered /
    refreshed local contacts prevent fallback. Dry-runs never mutate ORM data.
    Caller commits and retains the returned state for rotation between runs.
    """
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run musí byť boolean.")
    profile = campaign.targeting_profile or {}
    previous = (campaign.last_run_summary or {}).get("salon_discovery_state", {})
    previous = previous if isinstance(previous, dict) else {}
    state = {str(key): str(value) for key, value in previous.items()
             if _source_url(key) and isinstance(value, str)}
    report = {"enabled": profile.get("salon_discovery") is True, "dry_run": dry_run,
              "searched": 0, "fetched": 0, "eligible": 0, "imported": 0,
              "refreshed": 0, "refreshable": 0,
              "skipped": 0, "skipped_directory": 0, "errors": [], "candidates": [], "state": state,
              "searched_locations": [], "fallback_used": False,
              "primary_queue_available": False, "tiers": []}
    if not report["enabled"]:
        return report
    primary = salon_locations(profile, include_fallback=False)
    if not primary:
        report["errors"].append("invalid_locations")
        return report
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit musí byť kladné celé číslo.")
    limit = min(limit, MAX_FETCHES)
    search, fetch = search or brave_web_search, fetch or safe_http_get
    primary_names = {_fold(item) for item in primary}
    fallback = [item for item in salon_locations(profile) if _fold(item) not in primary_names]
    now = datetime.now(timezone.utc)
    if fallback:
        with db.session.no_autoflush:
            report["primary_queue_available"] = _has_usable_local_queue(campaign, primary, now)
    seen_emails = set()
    for tier_name, locations in [("primary", primary), ("fallback", fallback)]:
        if not locations:
            continue
        if tier_name == "fallback":
            if report["eligible"] or report["primary_queue_available"]:
                break
            report["fallback_used"] = True
        tier = _discover_salon_tier(locations, dry_run=dry_run, search=search, fetch=fetch,
                                   limit=limit, state=state, seen_emails=seen_emails, now=now)
        report["tiers"].append({"tier": tier_name, **{
            key: value for key, value in tier.items() if key != "candidates"
        }})
        for key in ("searched", "fetched", "eligible", "imported", "refreshed", "refreshable", "skipped",
                    "skipped_directory"):
            report[key] += tier[key]
        for key in ("errors", "candidates", "searched_locations"):
            report[key].extend(tier[key])
    report["state"] = dict(sorted(state.items(), key=lambda pair: pair[1])[-MAX_STATE_URLS:])
    if not dry_run:
        campaign.last_run_summary = {**(campaign.last_run_summary or {}),
                                     "salon_discovery_state": report["state"]}
    return report


def _discover_salon_tier(locations, *, dry_run, search, fetch, limit, state, seen_emails, now):
    """One bounded pass; interleave towns before applying persisted rotation."""
    report = {"locations": locations, "searched_locations": [],
              "searched": 0, "fetched": 0, "eligible": 0, "imported": 0,
              "refreshed": 0, "refreshable": 0, "skipped": 0, "skipped_directory": 0,
              "errors": [], "candidates": []}
    city_urls = []
    for location in locations:
        # Fixed factual query; no LLM-generated search or arbitrary user commands.
        query = f'"{location}" salón kaderníctvo kozmetika kontakt email objednanie'
        try:
            data = search(query, count=20)
            results = data.get("web", {}).get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                raise ValueError("invalid_results")
            report["searched"] += 1
            report["searched_locations"].append(location)
            urls = []
            for result in results[:20]:
                raw_url = _url(result.get("url")) if isinstance(result, dict) else None
                if raw_url and _unsupported_directory(raw_url):
                    report["skipped_directory"] += 1
                    continue
                url = _source_url(raw_url)
                if url and url not in urls:
                    urls.append(url)
            city_urls.append(urls)
        except Exception:
            report["errors"].append("search_unavailable")
    urls = list(dict.fromkeys(url for row in zip_longest(*city_urls) for url in row if url))
    # Retain our own unsent queue even when a search engine stops returning its
    # source. Revalidation still uses the same bounded public fetch budget.
    with db.session.no_autoflush:
        stale_candidates = CompanyContact.query.join(Company).filter(
            CompanyContact.source_type == SOURCE_TYPE,
            CompanyContact.contact_type == 'email', CompanyContact.is_verified.is_(True),
            Company.municipality.in_(locations),
            or_(CompanyContact.last_verified_at.is_(None),
                CompanyContact.last_verified_at < (now - timedelta(days=6)).replace(tzinfo=None)),
        ).order_by(CompanyContact.last_verified_at, CompanyContact.id).limit(120).all()
        stale_urls = []
        for contact in stale_candidates:
            if _stale_uncontacted_contact([contact], now) is not None:
                url = _source_url(contact.source_url)
                if url and url not in stale_urls:
                    stale_urls.append(url)
                if len(stale_urls) >= limit:
                    break
    urls = list(dict.fromkeys([*stale_urls, *urls]))
    # Fresh candidates first, then oldest checked. Failed pages cannot starve
    # later candidates on every run; rejected business data is never imported.
    urls.sort(key=lambda url: (url not in stale_urls, url in state, state.get(url, "")))
    for url in urls:
        if report["fetched"] >= limit:
            break
        with db.session.no_autoflush:
            existing = CompanyContact.query.filter(CompanyContact.source_url == url).all()
            existing_source = CompanySource.query.filter(CompanySource.resource_url == url).first()
        if existing or existing_source:
            with db.session.no_autoflush:
                stale = _stale_uncontacted_contact(existing, now)
                refresh_target = _contact_snapshot(stale) if stale is not None and dry_run else stale
            if refresh_target is None:
                report["skipped"] += 1
                continue
            report["fetched"] += 1
            state[url] = now.isoformat()
            if recheck_salon_contact(refresh_target, locations, fetch=fetch):
                report["refreshable"] += 1
                report["refreshed"] += int(not dry_run)
                report["eligible"] += 1
                refreshed_evidence = refresh_target.company.analysis_evidence[-1]
                report["candidates"].append({key: refreshed_evidence[key] for key in (
                    "name", "municipality", "street", "postal_code", "email", "source_url",
                    "website_url", "name_kind", "booking_signal", "quote",
                )})
                seen_emails.add(refreshed_evidence["email"])
            else:
                report["errors"].append("source_recheck_failed")
            continue
        report["fetched"] += 1
        state[url] = now.isoformat()
        try:
            response = fetch(url, timeout=20, max_bytes=4_000_000)
            final_url = _source_url(response.url)
            if response.status_code != 200 or not final_url or _host(final_url) != _host(url):
                raise ValueError("source_unavailable")
            candidate = parse_salon_listing(response.text, final_url, locations)
        except Exception:
            report["errors"].append("source_unavailable")
            continue
        if candidate is None:
            report["skipped"] += 1
            continue
        with db.session.no_autoflush:
            skip = _duplicate_or_suppressed(candidate)
        if skip or candidate["email"] in seen_emails:
            report["skipped"] += 1
            continue
        seen_emails.add(candidate["email"])
        report["eligible"] += 1
        report["candidates"].append(candidate)
        if dry_run:
            continue
        evidence = {**candidate, "source_type": SOURCE_TYPE, "checked_at": now.isoformat(),
                    "limitation": "Observed appointment method on this page; not global booking absence."}
        company = Company(
            official_name=candidate["name"], municipality=candidate["municipality"],
            street=candidate["street"], postal_code=candidate["postal_code"], country="Slovensko",
            company_type="Salón", contacts_checked_at=now,
            analysis_reason="Verejný názov prevádzky; právny prevádzkovateľ a IČO neboli overené.",
            analysis_evidence=[evidence],
        )
        company.contacts.append(CompanyContact(
            contact_type="email", value=candidate["email"], source_type=SOURCE_TYPE,
            source_url=candidate["source_url"], is_verified=True, is_primary=True,
            last_verified_at=now.replace(tzinfo=None), confidence_score=1.0,
            label="Verejný e-mail prevádzky",
        ))
        if candidate["website_url"]:
            company.contacts.append(CompanyContact(
                contact_type="website", value=candidate["website_url"], source_type=SOURCE_TYPE,
                source_url=candidate["source_url"], is_verified=True,
                last_verified_at=now.replace(tzinfo=None),
            ))
        db.session.add(company)
        db.session.flush()
        report["imported"] += 1
    return report
