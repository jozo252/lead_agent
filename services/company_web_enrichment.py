from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from extensions import db
from models import Company
from services.safe_http import SafeHttpError, safe_http_get


MAX_PAGES_PER_COMPANY = 6
MAX_PAGE_TEXT_LENGTH = 6_000
RELEVANT_PAGE_KEYWORDS = (
    "sluzb",
    "služb",
    "realiz",
    "referenc",
    "o-nas",
    "o-nás",
    "about",
    "karier",
    "kariér",
    "career",
    "kontakt",
    "contact",
)
SUBCONTRACTOR_NEED_VALUES = {"low", "medium", "high"}


COMPANY_WEB_ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "company_type",
        "services",
        "markets",
        "works_abroad",
        "regions",
        "employee_count",
        "employee_count_source",
        "subcontractor_need",
        "outreach_relevant",
        "analysis_reason",
        "analysis_evidence",
    ],
    "properties": {
        "company_type": {"type": ["string", "null"]},
        "services": {"type": ["array", "null"], "items": {"type": "string"}},
        "markets": {"type": ["array", "null"], "items": {"type": "string"}},
        "works_abroad": {"type": ["boolean", "null"]},
        "regions": {"type": ["array", "null"], "items": {"type": "string"}},
        "employee_count": {"type": ["integer", "null"]},
        "employee_count_source": {"type": ["string", "null"]},
        "subcontractor_need": {
            "type": ["string", "null"],
            "enum": ["low", "medium", "high", None],
        },
        "outreach_relevant": {"type": ["boolean", "null"]},
        "analysis_reason": {"type": ["string", "null"]},
        "analysis_evidence": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["quote", "source_url"],
                "properties": {
                    "quote": {"type": "string"},
                    "source_url": {"type": "string"},
                },
            },
        },
    },
}


def normalize_website_url(url: str | None) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    return url


def base_domain(url: str | None) -> str | None:
    normalized_url = normalize_website_url(url)
    if not normalized_url:
        return None

    try:
        domain = urlparse(normalized_url).hostname
    except ValueError:
        return None

    if not domain:
        return None

    return domain.lower().removeprefix("www.")


def extract_page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    return " ".join(soup.get_text(" ", strip=True).split())


def get_company_website(company: Company) -> str | None:
    website_contacts = [
        contact
        for contact in company.contacts
        if contact.contact_type == "website"
    ]
    website_contacts.sort(
        key=lambda contact: (
            bool(contact.is_verified),
            bool(contact.is_primary),
            contact.confidence_score or 0,
        ),
        reverse=True,
    )

    for contact in website_contacts:
        website = normalize_website_url(contact.value)
        if website:
            return website

    return None


def collect_relevant_pages(
    website_url: str,
    maximum_pages: int = MAX_PAGES_PER_COMPANY,
) -> list[dict[str, str]]:
    """Stiahne domovskú stránku a najrelevantnejšie interné odkazy."""
    website_url = normalize_website_url(website_url)
    if not website_url:
        return []

    root_domain = base_domain(website_url)
    if not root_domain:
        return []

    headers = {"User-Agent": "LeadAgent-WebEnrichment/1.0"}

    try:
        root_response = safe_http_get(website_url, headers=headers, timeout=15)
        if root_response.status_code >= 400:
            return []
    except SafeHttpError:
        return []

    pages = [{
        "url": root_response.url,
        "text": extract_page_text(root_response.text)[:MAX_PAGE_TEXT_LENGTH],
    }]
    soup = BeautifulSoup(root_response.text, "html.parser")
    candidate_urls = []

    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        label = " ".join(link.get_text(" ", strip=True).split()).lower()
        absolute_url = urljoin(root_response.url, href)

        if base_domain(absolute_url) != root_domain:
            continue

        url_path = urlparse(absolute_url).path.lower()
        if not any(keyword in label or keyword in url_path for keyword in RELEVANT_PAGE_KEYWORDS):
            continue

        normalized_url = absolute_url.split("#", 1)[0]
        if normalized_url not in candidate_urls:
            candidate_urls.append(normalized_url)

    for candidate_url in candidate_urls[: max(0, maximum_pages - 1)]:
        try:
            response = safe_http_get(candidate_url, headers=headers, timeout=15)
            if response.status_code >= 400:
                continue
        except SafeHttpError:
            continue

        pages.append({
            "url": response.url,
            "text": extract_page_text(response.text)[:MAX_PAGE_TEXT_LENGTH],
        })

    return [page for page in pages if page["text"]]


def build_analysis_context(pages: list[dict[str, str]]) -> str:
    return "\n\n".join(
        f"ZDROJ: {page['url']}\nTEXT: {page['text']}"
        for page in pages
    )


def clean_string(value) -> str | None:
    if not isinstance(value, str):
        return None

    value = " ".join(value.split())
    return value or None


def clean_string_list(value) -> list[str] | None:
    if not isinstance(value, list):
        return None

    cleaned = []
    for item in value:
        normalized_item = clean_string(item)
        if normalized_item and normalized_item not in cleaned:
            cleaned.append(normalized_item)

    return cleaned or None


def normalize_company_analysis(data: dict) -> dict:
    """Normalizuje AI odpoveď; chýbajúce alebo neisté dáta ponechá prázdne."""
    if not isinstance(data, dict):
        raise ValueError("AI analýza nemá objektový JSON formát.")

    employee_count = data.get("employee_count")
    if not isinstance(employee_count, int) or not 0 < employee_count <= 1_000_000:
        employee_count = None

    subcontractor_need = data.get("subcontractor_need")
    if subcontractor_need not in SUBCONTRACTOR_NEED_VALUES:
        subcontractor_need = None

    evidence = []
    for item in data.get("analysis_evidence") or []:
        if not isinstance(item, dict):
            continue

        quote = clean_string(item.get("quote"))
        source_url = normalize_website_url(item.get("source_url"))
        if quote and source_url:
            evidence.append({"quote": quote, "source_url": source_url})

    outreach_relevant = (
        data.get("outreach_relevant")
        if isinstance(data.get("outreach_relevant"), bool)
        else None
    )

    return {
        "company_type": clean_string(data.get("company_type")),
        "services": clean_string_list(data.get("services")),
        "markets": clean_string_list(data.get("markets")),
        "works_abroad": data.get("works_abroad") if isinstance(data.get("works_abroad"), bool) else None,
        "regions": clean_string_list(data.get("regions")),
        "employee_count": employee_count,
        "employee_count_source": clean_string(data.get("employee_count_source")) if employee_count else None,
        "subcontractor_need": subcontractor_need,
        "outreach_relevant": outreach_relevant,
        "analysis_reason": (
            clean_string(data.get("analysis_reason"))
            if outreach_relevant is not None
            else None
        ),
        "analysis_evidence": evidence or None,
    }


def analyze_company_pages(company: Company, pages: list[dict[str, str]]) -> dict:
    """Vráti štruktúrovaný obchodný profil len z textu stiahnutého z webu."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Chýba OPENAI_API_KEY.")

    from openai import OpenAI

    prompt = f"""
Analyzuj web firmy a vráť iba JSON podľa schémy.

Firma z RPO:
- názov: {company.official_name or 'neznámy'}
- IČO: {company.ico or 'neznáme'}
- obec: {company.municipality or 'neznáma'}
- SK NACE: {company.sk_nace_code or 'neznámy'} {company.sk_nace_name or ''}

Pravidlá:
- Použi výlučne priložený text webu; nič nedohaduj.
- Ak údaj nie je výslovne doložený, vráť null. Pri zoznamoch vráť null, nie prázdny zoznam.
- employee_count uveď iba pri výslovnom počte pracovníkov na webe.
- employee_count_source je stručný citát alebo URL s týmto počtom.
- services, markets a regions uvádzaj stručne po slovensky.
- subcontractor_need povoľ iba low, medium, high, ak web obsahuje dôkaz (kariéra, rast, projekty, hľadanie ľudí alebo subdodávateľov).
- analysis_evidence obsahuje najviac 5 krátkych doslovných citátov a URL, z ktorej citát pochádza.

TEXT WEBU:
{build_analysis_context(pages)}
"""

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=os.environ.get("OPENAI_COMPANY_ANALYSIS_MODEL", "gpt-4.1-mini"),
        messages=[
            {
                "role": "system",
                "content": "Si presný analytik webových stránok. Nehalucinuješ a pracuješ iba s poskytnutými zdrojmi.",
            },
            {"role": "user", "content": prompt},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "company_web_analysis",
                "strict": True,
                "schema": COMPANY_WEB_ANALYSIS_SCHEMA,
            },
        },
    )

    content = response.choices[0].message.content
    return normalize_company_analysis(json.loads(content))


def apply_company_analysis(company: Company, analysis: dict) -> None:
    """Zapíše štruktúrované webové dáta; null hodnoty ponechá prázdne."""
    company.company_type = analysis["company_type"]
    company.services = analysis["services"]
    company.markets = analysis["markets"]
    company.works_abroad = analysis["works_abroad"]
    company.regions = analysis["regions"]
    company.employee_count = analysis["employee_count"]
    company.employee_count_source = analysis["employee_count_source"]
    company.subcontractor_need = analysis["subcontractor_need"]
    company.outreach_relevant = analysis["outreach_relevant"]
    company.analysis_reason = analysis["analysis_reason"]
    company.analysis_evidence = analysis["analysis_evidence"]
    company.website_analyzed_at = datetime.now(timezone.utc)


def enrich_company_from_website(company: Company) -> dict:
    website = get_company_website(company)
    if not website:
        raise ValueError("Firma nemá uložený web.")

    pages = collect_relevant_pages(website)
    if not pages:
        raise ValueError("Z webu sa nepodarilo získať použiteľný text.")

    analysis = analyze_company_pages(company, pages)
    apply_company_analysis(company, analysis)

    return {
        "website": website,
        "pages": len(pages),
        "analysis": analysis,
    }


def enrich_company_websites(
    max_companies=20,
    include_analyzed=False,
    ico=None,
) -> dict:
    if ico:
        company = Company.query.filter_by(ico=str(ico).strip()).one_or_none()
        if company is None:
            raise ValueError(f"Firma s IČO {ico} nie je v databáze.")
        companies = [company]
    else:
        query = Company.query.order_by(Company.created_at.desc())

        if not include_analyzed:
            query = query.filter(Company.website_analyzed_at.is_(None))

        companies = query.limit(max_companies).all()
    summary = {"processed_companies": 0, "analyzed_companies": 0, "errors": []}

    for company in companies:
        try:
            enrich_company_from_website(company)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            summary["errors"].append({"ico": company.ico, "error": str(exc)})
            continue

        summary["processed_companies"] += 1
        summary["analyzed_companies"] += 1

    return summary
