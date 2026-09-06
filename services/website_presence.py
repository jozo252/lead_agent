"""Bounded, evidence-backed website searches; absence in a DB is not absence online."""

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import and_, func

from extensions import db
from models import Company, CompanyContact, CompanyWebsiteCheck
from services.opportunity_scout import brave_web_search, normalize_source_url
from services.rpo_sync import (
    BLOCKED_DOMAINS,
    DIRECTORY_DOMAINS,
    SOCIAL_DOMAINS,
    strip_legal_suffix,
)


WEBSITE_CHECK_MAX_AGE_DAYS = 30
THIRD_PARTY_DOMAINS = BLOCKED_DOMAINS | DIRECTORY_DOMAINS | SOCIAL_DOMAINS | {
    "bazos.sk", "bazos.cz", "orsr.sk", "zrsr.sk", "rpo.statistics.sk",
    "statistics.sk", "registeruz.sk", "firmy.cz", "youtube.com",
    "x.com", "twitter.com", "azet.sk", "finstat.sk", "podnikam.sk",
}
STATUS_LABELS = {
    "unknown": "Web nie je spoľahlivo overený",
    "found": "Web alebo kandidát na web nájdený",
    "not_found": "Web sa pri overení nenašiel (nie dôkaz neexistencie)",
    "error": "Overenie webu zlyhalo",
}


def _naive_utc(value):
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _now(value=None):
    return _naive_utc(value or datetime.now(timezone.utc))


def _stored_websites(company):
    return [
        contact for contact in company.contacts
        if str(contact.contact_type or "").strip().casefold() == "website"
    ]


def _is_fresh(check, now):
    checked_at = _naive_utc(check.checked_at) if check else None
    return bool(checked_at and now - timedelta(days=WEBSITE_CHECK_MAX_AGE_DAYS) <= checked_at <= now)


def is_website_absence_eligible(company, *, now=None):
    check = company.website_check
    return bool(
        not _stored_websites(company)
        and check is not None
        and check.status == "not_found"
        and _is_fresh(check, _now(now))
    )


def website_absence_filter(*, now=None):
    """Identical to the in-memory gate, evaluated by the database before LIMIT."""
    current = _now(now)
    return and_(
        ~Company.contacts.any(func.lower(func.trim(CompanyContact.contact_type)) == "website"),
        Company.website_check.has(and_(
            CompanyWebsiteCheck.status == "not_found",
            CompanyWebsiteCheck.checked_at >= current - timedelta(days=WEBSITE_CHECK_MAX_AGE_DAYS),
            CompanyWebsiteCheck.checked_at <= current,
        )),
    )


def website_presence_summary(company, *, now=None):
    check = company.website_check
    websites = _stored_websites(company)
    status = "found" if websites else (check.status if check else "unknown")
    if status not in STATUS_LABELS:
        status = "unknown"
    stale = bool(check and not _is_fresh(check, _now(now)))
    return {
        "status": status,
        "label": STATUS_LABELS[status] + ("; overenie je neaktuálne" if stale else ""),
        "eligible": is_website_absence_eligible(company, now=now),
        "checked_at": check.checked_at if check else None,
        "stale": stale,
        "evidence": check.evidence or [] if check else [],
        "searched_queries": check.searched_queries or [] if check else [],
        "website_url": websites[0].value if websites else (check.website_url if check else None),
        "last_error": check.last_error if check else None,
    }


def _normalized(value):
    value = unicodedata.normalize("NFKD", str(value or "")).casefold()
    value = "".join(character for character in value if not unicodedata.combining(character))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def _contains_phrase(text, value):
    phrase = _normalized(value)
    return bool(phrase and f" {phrase} " in f" {_normalized(text)} ")


def _identity_queries(company):
    name = " ".join(str(company.official_name or "").replace('"', "").split())[:200]
    locality = " ".join(str(company.municipality or "").replace('"', "").split())[:100]
    ico = re.sub(r"\D", "", str(company.ico or ""))
    if len(_normalized(strip_legal_suffix(name))) < 3 or (not locality and len(ico) != 8):
        return []
    queries = [f'"{name}" {locality}'.strip()]
    if len(ico) == 8:
        queries.append(f'"{ico}"')
    queries.append(f'"{strip_legal_suffix(name)}" {locality} web kontakt'.strip())
    return list(dict.fromkeys(queries))[:3]


def _third_party(url):
    host = (urlparse(url).hostname or "").casefold().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in THIRD_PARTY_DOMAINS)


def _identity_match(company, result):
    text = " ".join([
        str(result.get("title") or ""), str(result.get("description") or ""),
        *[str(item) for item in result.get("extra_snippets", [])],
    ])
    ico = re.sub(r"\D", "", str(company.ico or ""))
    ico_match = bool(len(ico) == 8 and re.search(r"(?<!\d)" + re.escape(ico) + r"(?!\d)", text))
    name = strip_legal_suffix(company.official_name or "")
    name_match = _contains_phrase(text, name)
    locality_match = _contains_phrase(text, company.municipality)
    strong = ico_match or (name_match and locality_match)
    return strong, name_match or ico_match, "ico" if ico_match else ("name_locality" if strong else "ambiguous")


def _validated_results(data):
    # Empty/malformed responses must never become a successful negative check.
    if not isinstance(data, dict) or not isinstance(data.get("web"), dict):
        raise ValueError("Neúplná odpoveď vyhľadávania.")
    results = data["web"].get("results")
    if not isinstance(results, list) or len(results) > 10:
        raise ValueError("Neúplný alebo nadlimitný zoznam výsledkov.")
    for result in results:
        if not isinstance(result, dict) or not normalize_source_url(result.get("url")):
            raise ValueError("Neplatný výsledok vyhľadávania.")
        if not isinstance(result.get("extra_snippets", []), list):
            raise ValueError("Neplatný formát dôkazov vyhľadávania.")
    return results


def check_company_website(company, *, search=None, now=None):
    """Update one check using <=3 queries. Caller owns commit; never fetch target pages.

    A negative check requires complete searches plus at least one identified
    third-party company result and no unresolved potential official website.
    Search metadata is evidence of a search, not proof a site does not exist.
    """
    check = company.website_check
    if check is None:
        check = CompanyWebsiteCheck(company=company)
        db.session.add(check)
    check.checked_at = _now(now)
    check.status = "unknown"
    check.evidence = []
    check.searched_queries = []
    check.website_url = None
    check.last_error = None
    websites = _stored_websites(company)
    if websites:
        check.status = "found"
        check.website_url = websites[0].value
        check.evidence = [{
            "kind": "stored_website", "url": contact.value,
            "source_url": contact.source_url, "verified": bool(contact.is_verified),
        } for contact in websites[:10]]
        return check

    queries = _identity_queries(company)
    if len(queries) < 2:
        check.last_error = "Chýba dostatočný názov a obec alebo osemmiestne IČO pre overenie identity."
        return check
    evidence = []
    executed = []
    identified_third_party = False
    uncertain_site = False
    found_site = None
    search = search or brave_web_search
    try:
        for query in queries:
            executed.append(query)
            for result in _validated_results(search(query, count=10, country="SK", search_lang="sk")):
                url = normalize_source_url(result["url"])
                strong, possible, reason = _identity_match(company, result)
                third_party = _third_party(url)
                if not possible and third_party:
                    continue
                # A site returned for an exact identity query may use a trading
                # name unlike the legal company name. Keep it unresolved instead
                # of silently treating a non-matching snippet as proof of absence.
                evidence.append({
                    "query": query, "url": url,
                    "title": str(result.get("title") or "")[:500],
                    "description": str(result.get("description") or "")[:1000],
                    "kind": "third_party" if third_party else "potential_website",
                    "identity_match": strong, "identity_reason": reason,
                })
                if third_party:
                    identified_third_party = identified_third_party or strong
                    # A directory snippet can point at an unvisited official site.
                    text = " ".join([str(result.get("description") or ""), *map(str, result.get("extra_snippets", []))])
                    for link in re.findall(r"(?:https?://|www\.)[^\s<>\"']+", text):
                        normalized_link = normalize_source_url(link if "://" in link else "https://" + link)
                        if normalized_link and not _third_party(normalized_link):
                            uncertain_site = True
                elif strong:
                    found_site = found_site or url
                else:
                    uncertain_site = True
        if found_site:
            check.status = "found"
            check.website_url = found_site
        elif identified_third_party and not uncertain_site:
            check.status = "not_found"
    except Exception:
        # Provider exceptions may contain credentials/request URLs; persist none of them.
        check.status = "error"
        check.last_error = "Vyhľadávanie sa nedokončilo spoľahlivo; skontrolujte konfiguráciu a skúste manuálne znova."
    check.evidence = evidence[:30]
    check.searched_queries = executed
    return check
