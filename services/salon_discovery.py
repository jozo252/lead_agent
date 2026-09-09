"""Bounded public salon discovery. Observed contacts, never email delivery.

``is_verified`` means the exact email was observed on the identified salon's
page. A telephone/email appointment signal is dated evidence, not proof that
the business has no booking system elsewhere. The caller owns the transaction.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from sqlalchemy import func, or_

from extensions import db
from models import Company, CompanyContact, CompanySource, Lead, OutboundEmail
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
                   "firmy.sk", "google.com", "finstat.sk"}


def _text(value):
    return " ".join(str(value or "").split())


def _fold(value):
    return "".join(c for c in unicodedata.normalize("NFKD", _text(value))
                   if not unicodedata.combining(c)).casefold()


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


def _source_url(value):
    url = _url(value)
    if not url or len(url) > 1500:
        return None
    host = _host(url)
    if _notino(url):
        if not re.fullmatch(r"/salony/[a-z0-9-]+/?", urlsplit(url).path):
            return None
        return "https://www.notino.sk" + urlsplit(url).path.rstrip("/") + "/"
    if any(host == domain or host.endswith("." + domain) for domain in DIRECTORY_HOSTS):
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
        "name": name, "municipality": municipality, "street": street,
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


def discover_salon_contacts(campaign, *, dry_run=True, search=None, fetch=None, limit=12):
    """Search <=6 municipalities and fetch <=12 pages. Caller commits explicitly.

    ``dry_run=True`` never writes or mutates the campaign. Persist/retain returned
    ``state`` in last_run_summary['salon_discovery_state'] for rotation between
    runs. A write run also sets that key itself; callers replacing their summary
    must preserve it. Exceptions from providers are deliberately not serialized.
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
              "skipped": 0, "errors": [], "candidates": [], "state": state}
    if not report["enabled"]:
        return report
    raw_locations = profile.get("location_keywords", [])
    if (not isinstance(raw_locations, list) or not raw_locations
            or len(raw_locations) > MAX_LOCATIONS
            or any(not isinstance(item, str) or not re.fullmatch(r"[\w .-]{2,60}", item)
                   for item in raw_locations)):
        report["errors"].append("invalid_locations")
        return report
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit musí byť kladné celé číslo.")
    limit = min(limit, MAX_FETCHES)
    search, fetch = search or brave_web_search, fetch or safe_http_get
    urls = []
    for location in dict.fromkeys(raw_locations):
        # Fixed factual query; no LLM-generated search or arbitrary user commands.
        query = f'"{location}" salón kaderníctvo kozmetika kontakt email objednanie'
        try:
            data = search(query, count=20)
            results = data.get("web", {}).get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                raise ValueError("invalid_results")
            report["searched"] += 1
            for result in results[:20]:
                url = _source_url(result.get("url")) if isinstance(result, dict) else None
                if url and url not in urls:
                    urls.append(url)
        except Exception:
            report["errors"].append("search_unavailable")
    # Fresh candidates first, then oldest checked. Failed pages cannot starve
    # later candidates on every run; rejected business data is never imported.
    urls.sort(key=lambda url: (url in state, state.get(url, "")))
    now = datetime.now(timezone.utc)
    seen_emails = set()
    for url in urls:
        if report["fetched"] >= limit:
            break
        with db.session.no_autoflush:
            existing = CompanyContact.query.filter(CompanyContact.source_url == url).first()
            existing_source = CompanySource.query.filter(CompanySource.resource_url == url).first()
        if existing or existing_source:
            report["skipped"] += 1
            continue
        report["fetched"] += 1
        state[url] = now.isoformat()
        try:
            response = fetch(url, timeout=20, max_bytes=4_000_000)
            final_url = _source_url(response.url)
            if response.status_code != 200 or not final_url or _host(final_url) != _host(url):
                raise ValueError("source_unavailable")
            candidate = parse_salon_listing(response.text, final_url, raw_locations)
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
    report["state"] = dict(sorted(state.items(), key=lambda pair: pair[1])[-MAX_STATE_URLS:])
    if not dry_run:
        campaign.last_run_summary = {**(campaign.last_run_summary or {}),
                                     "salon_discovery_state": report["state"]}
    return report
