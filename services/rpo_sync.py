from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from requests import Response
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from extensions import db
from models import Company, CompanyActivity, CompanySource, SyncState
import os
from dotenv import load_dotenv
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


RPO_BASE_URL = "https://datahub.ekosystem.slovensko.digital"
RPO_SYNC_URL = (
    f"{RPO_BASE_URL}/api/data/rpo2/organizations/sync"
)

SYNC_NAME = "rpo2_organizations"


class RpoSyncError(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_http_session() -> requests.Session:
    """
    Session s retry mechanizmom.

    Retryujeme iba chyby, ktoré majú zmysel skúsiť znovu:
    rate limit a dočasné serverové chyby.
    """

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=5,
        pool_maxsize=5,
    )

    session = requests.Session()
    session.mount("https://", adapter)
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "LeadAgent-RPO-Sync/1.0",
        }
    )

    return session


def replace_company_activities(
    company: Company,
    record: dict[str, Any],
) -> None:
    payload = record.get("data")

    if not isinstance(payload, dict):
        return

    activities = payload.get("activities")

    if not isinstance(activities, list):
        activities = []

    company.activities.clear()

    seen_descriptions: set[str] = set()

    for activity in activities:
        if not isinstance(activity, dict):
            continue

        description = activity.get(
            "economicActivityDescription"
        )

        if not description:
            continue

        description = str(description).strip()

        if not description:
            continue

        deduplication_key = description.casefold()

        if deduplication_key in seen_descriptions:
            continue

        seen_descriptions.add(deduplication_key)

        company.activities.append(
            CompanyActivity(
                description=description,
                valid_from=parse_date(
                    activity.get("validFrom")
                ),
                valid_to=parse_date(
                    activity.get("validTo")
                ),
            )
        )
        
def get_or_create_sync_state() -> SyncState:
    state = SyncState.query.filter_by(name=SYNC_NAME).one_or_none()

    if state is None:
        state = SyncState(
            name=SYNC_NAME,
            status="idle",
            processed_records=0,
        )
        db.session.add(state)
        db.session.commit()

    return state


def datetime_to_iso(value: datetime) -> str:
    """
    API očakáva ISO 8601.

    Napríklad:
    2026-07-22T18:30:00.000000Z
    """

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    value = value.astimezone(timezone.utc)

    return value.isoformat().replace("+00:00", "Z")


def parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None

    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed
    except ValueError:
        return None


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value

    return None


def get_nested(data: dict[str, Any], *path: str) -> Any:
    current: Any = data

    for key in path:
        if not isinstance(current, dict):
            return None

        current = current.get(key)

    return current
def parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None

    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def normalize_ico(value: Any) -> str | None:
    if value is None:
        return None

    digits = "".join(
        character
        for character in str(value)
        if character.isdigit()
    )

    if not digits:
        return None

    return digits.zfill(8) if len(digits) <= 8 else digits


def select_current_record(
    records: Any,
) -> dict[str, Any] | None:
    """
    Vyberie aktuálne platný záznam.

    Priorita:
    1. záznam bez validTo,
    2. najnovší podľa validFrom.
    """

    if not isinstance(records, list):
        return None

    valid_records = [
        record
        for record in records
        if isinstance(record, dict)
    ]

    if not valid_records:
        return None

    current_records = [
        record
        for record in valid_records
        if not record.get("validTo")
    ]

    candidates = current_records or valid_records

    return max(
        candidates,
        key=lambda record: record.get("validFrom") or "",
    )


def extract_codelist_value(value: Any) -> str | None:
    """
    Podporuje:
    {"value": "Bratislava"}

    aj:
    {"value": {"code": "105", "value": "Slobodné povolanie"}}
    """

    if value is None:
        return None

    if isinstance(value, str):
        return value.strip() or None

    if not isinstance(value, dict):
        return str(value).strip() or None

    nested_value = value.get("value")

    if isinstance(nested_value, dict):
        nested_value = nested_value.get("value")

    if nested_value is None:
        return None

    return str(nested_value).strip() or None


def build_street_address(
    address: dict[str, Any] | None,
) -> str | None:
    if not address:
        return None

    street = address.get("street")
    building_number = address.get("buildingNumber")

    parts = [
        str(part).strip()
        for part in (street, building_number)
        if part not in (None, "")
    ]

    return " ".join(parts) or None


def extract_postal_code(
    address: dict[str, Any] | None,
) -> str | None:
    if not address:
        return None

    postal_codes = address.get("postalCodes")

    if isinstance(postal_codes, list) and postal_codes:
        return str(postal_codes[0]).strip() or None

    if isinstance(postal_codes, str):
        return postal_codes.strip() or None

    return None


def normalized_rpo_fields(
    record: dict[str, Any],
) -> dict[str, Any]:
    payload = record.get("data")

    if not isinstance(payload, dict):
        payload = {}

    current_name = select_current_record(
        payload.get("fullNames")
    )
    current_identifier = select_current_record(
        payload.get("identifiers")
    )
    current_address = select_current_record(
        payload.get("addresses")
    )
    current_legal_form = select_current_record(
        payload.get("legalForms")
    )

    official_name = (
        current_name.get("value")
        if current_name
        else None
    )

    ico = (
        current_identifier.get("value")
        if current_identifier
        else None
    )

    municipality = None
    country = None

    if current_address:
        municipality = extract_codelist_value(
            current_address.get("municipality")
        )
        country = extract_codelist_value(
            current_address.get("country")
        )

    legal_form = None

    if current_legal_form:
        legal_form = extract_codelist_value(
            current_legal_form.get("value")
        )

    establishment = payload.get("establishment")
    termination = payload.get("termination")

    return {
        "ico": normalize_ico(ico),
        "official_name": (
            str(official_name).strip()
            if official_name
            else None
        ),
        "status": (
            "terminated"
            if termination
            else "active"
        ),
        "legal_form": legal_form,
        "municipality": municipality,
        "postal_code": extract_postal_code(
            current_address
        ),
        "street": build_street_address(
            current_address
        ),
        "country": country,
        "established_on": parse_date(establishment),
        "terminated_on": parse_date(termination),
    }







def extract_source_register_name(record):
    payload = record.get("data")
    if not isinstance(payload,dict):
        payload={}
    sourceRegister=payload.get("sourceRegister")
    if not isinstance(sourceRegister,dict):
        sourceRegister={}
    firstvalue=sourceRegister.get("value")
    if isinstance(firstvalue,dict):
        result = str(firstvalue.get("value")).strip() or None
        return result
    else:
        return None


    
{
    "street": "Saratovská",
    "buildingNumber": "6B"
}
def build_street_address(address):
    if not isinstance(address, dict):
        return None

    parts = [
        address.get("street"),
        address.get("buildingNumber"),
    ]

    result = " ".join(
        part.strip()
        for part in parts
        if part
    )

    return result or None

def extract_postal_code(address):
    if not isinstance(address, dict):
        return None

    postal_codes = address.get("postalCodes")

    if not postal_codes:
        return None

    return postal_codes[0]


record = {
    "data": {
        "addresses": [
            {
                "street": "Stará",
                "buildingNumber": "1",
                "validTo": "2022-01-01",
                "municipality": {"value": "Poprad"},
                "postalCodes": ["05801"],
                "country": {"value": "Slovenská republika"},
            },
            {
                "street": "Nová",
                "buildingNumber": "15",
                "validFrom": "2022-01-02",
                "municipality": {"value": "Košice"},
                "postalCodes": ["04001"],
                "country": {"value": "Slovenská republika"},
            },
        ]
    }
}

{
    "municipality": "Košice",
    "postal_code": "04001",
    "street": "Nová 15",
    "country": "Slovenská republika",
}
def extract_address(record):
    payload = record.get("data")

    if not isinstance(payload, dict):
        payload = {}

    current_address = select_current_record(
        payload.get("addresses")
    )

    if not current_address:
        return None

    municipality = extract_codelist_value(
        current_address.get("municipality")
    )

    postal_code = extract_postal_code(
        current_address
    )

    street = build_street_address(
        current_address
    )

    country = extract_codelist_value(
        current_address.get("country")
    )
    if not municipality and not postal_code and not street and not country:
        return {
            "municipality": None,
            "postal_code": None,    
            "street": None,
            "country": None,
        }

    return {
        "municipality": municipality,
        "postal_code": postal_code,
        "street": street,
        "country": country,
    }

def extract_source_register_name(record):
    payload = record.get("data")
    if not isinstance(payload, dict):
        payload = {}
    payload = payload.get("sourceRegister")
    if isinstance(payload, dict):
        register = extract_codelist_value(payload.get("value"))
        return register
    return None
    

IGNORED_RPO_REGISTERS = {
    "Zoznam znalcov",
    "Zoznam prekladateľov",
    "Zoznam tlmočníkov",
}


def should_skip_record(normalized):
    ico = normalized.get("ico")
    official_name = normalized.get("official_name")

    if not ico and not official_name:
        return True

    return False

def should_skip_rpo_record(normalized, source_register=None):
    #normalized = normalized_rpo_fields(record)
    
    if source_register in IGNORED_RPO_REGISTERS:
        return True

    return should_skip_record(normalized)

def has_identity_conflict(company,normalized) -> bool:
    company_ico=company.ico or None
    normalized_ico=normalized.get("ico") or None
    if not company_ico or not normalized_ico:
        return False
    if company_ico != normalized_ico:
        return True
    return False


def choose_company_for_record(
    existing_source,
    existing_company_by_ico,
):
    if existing_source:
        return existing_source.company
    if existing_company_by_ico:
        return existing_company_by_ico
    return None


def ensure_company(
    existing_company,
    normalized,
):
    if existing_company:
        return existing_company
    ico=normalized.get("ico")
    official_name=normalized.get("official_name")
    return Company(
        ico=ico,
        official_name=official_name

    )
def update_company_from_normalized(company, normalized):
    company.ico = normalized.get("ico")
    company.official_name = normalized.get("official_name")
    company.status = normalized.get("status")
    company.legal_form = normalized.get("legal_form")
    company.municipality = normalized.get("municipality")
    company.postal_code = normalized.get("postal_code")
    company.street = normalized.get("street")
    company.country = normalized.get("country")
    company.established_on = normalized.get("established_on")
    company.terminated_on = normalized.get("terminated_on")

    return company
  
def upsert_company(record):
    normalized = normalized_rpo_fields(record)

    source_register = extract_source_register_name(record)

    if should_skip_rpo_record(
        normalized,
        source_register
    ):
        return None

    rpo_id = record.get("id")

    existing_source = CompanySource.query.filter_by(
        source_type="rpo2",
        external_id=str(rpo_id),
    ).one_or_none()

    existing_company_by_ico = None

    if not existing_source:
        ico = normalized.get("ico")

        if ico:
            existing_company_by_ico = (
                Company.query
                .filter_by(ico=ico)
                .one_or_none()
            )

    company = choose_company_for_record(
        existing_source,
        existing_company_by_ico,
    )

    company = ensure_company(
        company,
        normalized,
    )

    if (
        existing_source
        and has_identity_conflict(company, normalized)
    ):
        raise ValueError(
            f"Identity conflict for RPO ID {rpo_id}"
        )

    update_company_from_normalized(
        company,
        normalized,
    )

    db.session.add(company)

    
    activities_data = extract_activities(record)
    replace_company_activities(company, activities_data)


    source = ensure_company_source(
        existing_source,
        company,
        record,
    )
    


    db.session.add(source)

    return company



def ensure_company_source(
    existing_source,
    company,
    record,
):
    if existing_source:
        existing_source.company = company
        existing_source.raw_data = record
        existing_source.external_id = str(record.get("id"))
        return existing_source

    # vytvor nový source
    return CompanySource(
        company=company,
        source_type="rpo2",
        external_id=str(record.get("id")),
        raw_data=record,
    )




#--------------------------------------ACTIVITIES----------------------------
record = {
    "data": {
        "activities": [
            {
                "economicActivityDescription": "Elektroinštalácie",
                "validFrom": "2020-01-01",
            },
            {
                "economicActivityDescription": "Montáž rozvádzačov",
                "validFrom": "2022-05-01",
                "validTo": "2025-01-01",
            },
        ]
    }
}


def extract_activities(record):
    if not isinstance(record, dict):
        record = {}

    data = record.get("data")

    if not isinstance(data, dict):
        data = {}

    activities = data.get("activities")

    if not isinstance(activities, list):
        return []

    result = []

    for activity in activities:
        if not isinstance(activity, dict):
            continue

        description = (
            activity.get("economicActivityDescription") or ""
        ).strip()

        if not description:
            continue

        valid_from = activity.get("validFrom")
        valid_to = activity.get("validTo")

        result.append({
            "description": description,
            "valid_from": parse_date(valid_from),
            "valid_to": parse_date(valid_to),
        })

    return result


def replace_company_activities(company, activities_data):
    company.activities.clear()

    if not isinstance(activities_data, list):
        return

    for activity_data in activities_data:
        if not isinstance(activity_data, dict):
            continue

        description = activity_data.get("description")

        if not description:
            continue

        activity = CompanyActivity(
            description=description,
            valid_from=activity_data.get("valid_from"),
            valid_to=activity_data.get("valid_to"),
        )

        company.activities.append(activity)

#------------------------------------------------------Company Contacts----------------------------------
contact_type = "facebook"
value = "  nejaká hodnota  "
def normalize_contact_value(contact_type,value):
     # skontroluj value
    if not value:
        return None
    value=str(value).lower().strip()
    return value

def strip_legal_suffix(name):
    suffixes = [
    "s.r.o.",
    "s. r. o.",
    "a.s.",
    "a. s.",
]
    name = name.strip()

    for suffix in suffixes:
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)].strip()
            break

    return name

#------------------------------------------------------Company Search Queries----------------------------------
def build_company_search_queries(company):
    search_queries=[]
    ico=company.ico
    official_name=company.official_name
    name = strip_legal_suffix(official_name) if official_name else None
    municipality=company.municipality if company.municipality else None
    if official_name and municipality:
        search_queries.append(f"{official_name} {municipality}")
    if name:
        search_queries.append(f"{name}")
    if ico:
        search_queries.append(f"{ico}")
    return search_queries



def normalize_search_results(raw_data):
    if not isinstance(raw_data,dict):
        raw_data={}
    web=raw_data.get("web")
    if not isinstance(web,dict):
        web={}
    results=web.get("results")
    normalized = []

    for result in results:
        if not isinstance(result, dict):
            continue

        url = result.get("url")

        if not url:
            continue

        normalized_result = {
            "title": result.get("title"),
            "url": url,
            "description": result.get("description"),
        }

        normalized.append(normalized_result)

    return normalized


def fetch_search_results(query):
    BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY")
    url = "https://api.search.brave.com/res/v1/web/search"

    if not isinstance(query, str):
        return {}

    query = query.strip()

    if not query:
        return {}

    if not BRAVE_API_KEY:
        logger.error("Missing BRAVE_API_KEY")
        return {}

    try:
        response = requests.get(
            url,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": BRAVE_API_KEY,
            },
            params={
                "q": query,
                "count": 10,
            },
            timeout=10,
        )

        # Vyhodí HTTPError pri 4xx/5xx odpovediach
        response.raise_for_status()

        return response.json()

    except requests.exceptions.JSONDecodeError as e:
        logger.error(f"Invalid JSON response from Brave API: {e}")

    except requests.RequestException as e:
        logger.error(f"Brave API request failed: {e}")

    return {}
   
BLOCKED_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "tiktok.com",
    "finstat.sk",
    "orsr.sk",
    "firmy.sk",
    "azet.sk",
    "google.com",
}


def extract_domain(url):
    if not isinstance(url, str):
        return None

    url = url.strip()

    if not url:
        return None

    # urlparse bez protokolu považuje doménu za cestu
    if "://" not in url:
        url = f"https://{url}"

    try:
        parsed = urlparse(url)
        domain = parsed.hostname

        if not domain:
            return None

        domain = domain.lower().strip(".")

        if domain.startswith("www."):
            domain = domain[4:]

        return domain

    except (ValueError, TypeError):
        return None


def is_blocked_domain(url):
    domain = extract_domain(url)

    if not domain:
        return True

    for blocked_domain in BLOCKED_DOMAINS:
        if domain == blocked_domain:
            return True

        if domain.endswith(f".{blocked_domain}"):
            return True

    return False
def filter_search_results(results):
    if not isinstance(results, list):
        return []

    filtered_results = []

    for result in results:
        if not isinstance(result, dict):
            continue

        url = result.get("url")

        if is_blocked_domain(url):
            continue

        filtered_results.append(result)

    return filtered_results

def search_company_web(query):
    raw_data = fetch_search_results(query)
    results = normalize_search_results(raw_data)
    results = filter_search_results(results)

    return results

def find_company_website(company):
    queries = build_company_search_queries(company)

    all_results = []

    for query in queries:
        results = search_company_web(query)
        all_results.extend(results)

    return duplicate_search_results(all_results)

def duplicate_search_results(results):
    if not isinstance(results, list):
        return []

    seen_urls = set()
    unique_results = []

    for result in results:
        if not isinstance(result, dict):
            continue

        url = result.get("url")
        domain = extract_domain(url)

        if domain in seen_urls:
            continue

        seen_urls.add(domain)
        unique_results.append(result)

    return unique_results



        
    

















def extract_next_url(response: Response) -> str | None:
    """
    requests vie spracovať Link header cez response.links.

    Očakávaný formát:
    Link: <https://...>; rel='next'
    """

    next_link = response.links.get("next")

    if not next_link:
        return None

    url = next_link.get("url")

    if not url:
        return None

    return urljoin(RPO_BASE_URL, url)


def extract_records(response_data: Any) -> list[dict[str, Any]]:
    """
    API pravdepodobne vracia JSON pole.

    Funkcia podporuje aj obalené odpovede, aby synchronizácia
    nespadla pri miernej zmene formátu.
    """

    if isinstance(response_data, list):
        return [
            item
            for item in response_data
            if isinstance(item, dict)
        ]

    if isinstance(response_data, dict):
        for key in ("items", "results", "data", "organizations"):
            value = response_data.get(key)

            if isinstance(value, list):
                return [
                    item
                    for item in value
                    if isinstance(item, dict)
                ]

    raise RpoSyncError(
        "Neznámy formát RPO odpovede. "
        f"Typ odpovede: {type(response_data).__name__}"
    )


def request_page(
    session: requests.Session,
    url: str,
) -> Response:
    response = session.get(
        url,
        timeout=(10, 60),
    )

    if response.status_code == 429:
        raise RpoSyncError(
            "RPO API odmietlo požiadavku pre prekročenie limitu."
        )

    if not response.ok:
        body_preview = response.text[:500]

        raise RpoSyncError(
            f"RPO API vrátilo HTTP {response.status_code}: "
            f"{body_preview}"
        )

    return response


def fetch_record_detail(
    session: requests.Session,
    record: dict[str, Any],
) -> dict[str, Any]:
    """
    Ak synchronizácia beží s only_ids, záznam obsahuje resource_url.
    Touto funkciou stiahneme celý detail.
    """

    resource_url = record.get("resource_url")

    if not resource_url:
        raise RpoSyncError(
            f"Záznam {record.get('id')} nemá resource_url."
        )

    detail_url = urljoin(RPO_BASE_URL, resource_url)

    response = request_page(session, detail_url)

    try:
        detail = response.json()
    except ValueError as exc:
        raise RpoSyncError(
            f"Detail záznamu {record.get('id')} nie je validný JSON."
        ) from exc

    if not isinstance(detail, dict):
        raise RpoSyncError(
            f"Detail záznamu {record.get('id')} nie je JSON objekt."
        )

    return detail

def update_company_from_rpo(
    *,
    company: Company,
    normalized: dict[str, Any],
    record: dict[str, Any],
) -> None:
    actualized_at = ensure_utc(
        parse_datetime(record.get("actualized_at"))
    )

    current_actualized_at = ensure_utc(
        company.rpo_actualized_at
    )

    should_update = (
        current_actualized_at is None
        or actualized_at is None
        or actualized_at >= current_actualized_at
    )

    if not should_update:
        return

    if normalized["ico"]:
        company.ico = normalized["ico"]

    company.official_name = normalized["official_name"]
    company.status = normalized["status"]
    company.legal_form = normalized["legal_form"]
    company.municipality = normalized["municipality"]
    company.postal_code = normalized["postal_code"]
    company.street = normalized["street"]
    company.country = normalized["country"]
    company.established_on = normalized["established_on"]
    company.terminated_on = normalized["terminated_on"]

    company.rpo_actualized_at = actualized_at
    company.rpo_updated_at = ensure_utc(
        parse_datetime(record.get("updated_at"))
    )


def upsert_rpo_record(record: dict[str, Any]) -> Company:
    rpo_id = record.get("id")

    if rpo_id is None:
        raise RpoSyncError("RPO záznam neobsahuje pole id.")

    try:
        rpo_id = int(rpo_id)
    except (TypeError, ValueError) as exc:
        raise RpoSyncError(
            f"Neplatné RPO ID: {rpo_id!r}"
        ) from exc

    normalized = normalized_rpo_fields(record)
    if not normalized["ico"] and not normalized["official_name"]:
        logger.warning(
            "RPO záznam %s nemá ani IČO ani oficiálny názov. "
            "Záznam sa ignoruje.",
            rpo_id,
        )
        return None
    ico = normalized["ico"]

    # 1. Poznáme už presne tento RPO záznam?
    source = CompanySource.query.filter_by(
        source_type="rpo2",
        external_id=str(rpo_id),
    ).one_or_none()

    if source is not None:
        company = source.company

    else:
        # 2. Nový RPO záznam, ale možno už poznáme firmu cez IČO.
        company = None

        if ico:
            company = Company.query.filter_by(
                ico=ico,
            ).one_or_none()

        # 3. Firma ešte vôbec neexistuje.
        if company is None:
            company = Company(
                ico=ico,
            )

            db.session.add(company)
            db.session.flush()

        source = CompanySource(
            company_id=company.id,
            source_type="rpo2",
            external_id=str(rpo_id),
            raw_data=record,
        )

        db.session.add(source)

    update_company_from_rpo(
        company=company,
        normalized=normalized,
        record=record,
    )

    source.company_id = company.id
    source.raw_data = record
    source.resource_url = (
        f"{RPO_BASE_URL}/api/data/rpo2/organizations/{rpo_id}"
    )
    source.source_updated_at = parse_datetime(
        record.get("updated_at")
    )
    source.fetched_at = utcnow()

    return company


def create_initial_sync_url(
    state: SyncState,
    only_ids: bool,
) -> str:
    """
    Pri prvom importe bez last_successful_sync_at zavolá celý sync.

    Pri ďalších behoch použije čas poslednej kompletne úspešnej
    synchronizácie.
    """

    params: list[str] = []

    if state.last_successful_sync_at:
        params.append(
            "since="
            + requests.utils.quote(
                datetime_to_iso(state.last_successful_sync_at),
                safe="",
            )
        )

    if only_ids:
        params.append("only_ids")

    if not params:
        return RPO_SYNC_URL

    return f"{RPO_SYNC_URL}?{'&'.join(params)}"


def sync_rpo(
    *,
    max_records: int | None = None,
    only_ids: bool = False,
    commit_every: int = 100,
    delay_seconds: float = 1.1,
    resume: bool = True,
) -> dict[str, Any]:
    """
    Synchronizuje RPO2 do lokálnej databázy.

    max_records:
        Testovací limit. None znamená bez limitu.

    only_ids:
        Sync endpoint vráti iba ID a následne sa sťahuje detail.
        Na prvý veľký import to nie je výhodné, pretože vznikne
        jeden extra request na každú firmu.

    commit_every:
        Počet záznamov na jednu DB transakciu.

    delay_seconds:
        Ochrana proti limitu 60 requestov/minútu.
        Pri plnej sync odpovedi stačí približne 1.1 sekundy
        medzi stránkami.

    resume:
        Ak existuje next_url z predošlého prerušeného behu,
        pokračuje z nej.
    """

    if max_records is not None and max_records <= 0:
        raise ValueError("max_records musí byť kladné číslo.")

    if commit_every <= 0:
        raise ValueError("commit_every musí byť kladné číslo.")

    state = get_or_create_sync_state()
    session = build_http_session()

    current_run_started_at = utcnow()

    if resume and state.next_url:
        url = state.next_url

        if state.sync_started_at is None:
            state.sync_started_at = current_run_started_at
    else:
        state.sync_started_at = current_run_started_at
        state.processed_records = 0
        state.next_url = None

        url = create_initial_sync_url(
            state=state,
            only_ids=only_ids,
        )

    state.status = "running"
    state.last_error = None
    db.session.commit()

    run_processed = 0
    page_number = 0

    try:
        while url:
            page_number += 1

            logger.info(
                "RPO sync page=%s url=%s",
                page_number,
                url,
            )

            response = request_page(session, url)

            try:
                response_data = response.json()
            except ValueError as exc:
                raise RpoSyncError(
                    "RPO API nevrátilo validný JSON."
                ) from exc

            records = extract_records(response_data)
            next_url = extract_next_url(response)

            for sync_record in records:
                full_record = sync_record

                if only_ids:
                    full_record = fetch_record_detail(
                        session,
                        sync_record,
                    )

                    # Detail endpoint je ďalší request.
                    time.sleep(delay_seconds)

                upsert_rpo_record(full_record)

                run_processed += 1
                state.processed_records += 1

                if run_processed % commit_every == 0:
                    db.session.commit()

                if (
                    max_records is not None
                    and run_processed >= max_records
                ):
                    # Zámerne neoznačíme synchronizáciu ako dokončenú.
                    # Pri testovacom limite sa nedá bezpečne pokračovať
                    # uprostred jednej stránky len pomocou next_url.
                    db.session.commit()

                    state.status = "partial"
                    state.last_error = (
                        "Synchronizácia zastavená cez max_records. "
                        "Testovací beh nie je plný checkpoint."
                    )
                    db.session.commit()

                    return {
                        "status": "partial",
                        "processed": run_processed,
                        "page": page_number,
                        "next_url": next_url,
                    }

            # Celá stránka bola bezpečne uložená.
            state.next_url = next_url
            db.session.commit()

            url = next_url

            if url:
                time.sleep(delay_seconds)

        completed_at = utcnow()

        state.status = "success"
        state.last_successful_sync_at = (
            state.sync_started_at or completed_at
        )
        state.sync_started_at = None
        state.next_url = None
        state.last_error = None

        db.session.commit()

        return {
            "status": "success",
            "processed": run_processed,
            "pages": page_number,
            "completed_at": datetime_to_iso(completed_at),
        }

    except Exception as exc:
        db.session.rollback()

        # State získame znovu, pretože rollback mohol expirovať objekt.
        state = get_or_create_sync_state()
        state.status = "failed"
        state.last_error = str(exc)[:5000]

        db.session.commit()

        logger.exception("RPO synchronizácia zlyhala.")

        raise

    finally:
        session.close()


