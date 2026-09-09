from __future__ import annotations

import gzip
import io
import json
import logging
import time
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from html import unescape
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import requests
from requests import Response
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from extensions import db
from models import Company, CompanyActivity, CompanyContact, CompanySource, SyncState
from services.safe_http import SafeHttpError, normalize_http_url, safe_http_get
import os
from dotenv import load_dotenv
from urllib.parse import urlparse
import unicodedata
import re
from pprint import pprint
logger = logging.getLogger(__name__)
load_dotenv()

RPO_BASE_URL = "https://datahub.ekosystem.slovensko.digital"
RPO_SYNC_URL = (
    f"{RPO_BASE_URL}/api/data/rpo2/organizations/sync"
)
RPO_EXPORT_BASE_URL = (
    "https://frkqbrydxwdp.compat.objectstorage.eu-frankfurt-1."
    "oraclecloud.com/susr-rpo/batch-init"
)

SYNC_NAME = "rpo2_organizations"
RPO_SOLE_TRADER_EXPORT_SYNC_NAME = "rpo2_sole_traders_export"
RPO_SOLE_TRADER_FILTER_VERSION = 2


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
            fetched_records=0,
            skipped_records=0,
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


def extract_sk_nace(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Vytiahne hlavnú SK NACE činnosť z RPO statisticalCodes."""
    statistical_codes = payload.get("statisticalCodes")

    if not isinstance(statistical_codes, dict):
        return None, None

    main_activity = statistical_codes.get("mainActivity")

    if not isinstance(main_activity, dict):
        return None, None

    code = main_activity.get("code")
    name = main_activity.get("value")

    return (
        str(code).strip() if code not in (None, "") else None,
        str(name).strip() if name not in (None, "") else None,
    )


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
    sk_nace_code, sk_nace_name = extract_sk_nace(payload)

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
        "sk_nace_code": sk_nace_code,
        "sk_nace_name": sk_nace_name,
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


SRO_LEGAL_FORMS = {
    "spoločnosť s ručením obmedzeným",
    "s.r.o.",
    "s. r. o.",
}

TRADE_REGISTER_NAMES = {
    "živnostenský register",
}

TARGET_SOLE_TRADER_NACE_PREFIXES = (
    "27",  # výroba elektrických zariadení
    "41",  # výstavba budov
    "42",  # inžinierske stavby
    "431",  # demolácie a príprava staveniska
    "433",  # kompletizácia a dokončovacie práce
)

TARGET_SOLE_TRADER_NACE_CODES = {
    "3312",  # oprava strojov
    "3314",  # oprava elektrických zariadení
    "3320",  # inštalácia priemyselných strojov a zariadení
    "4321",  # elektroinštalačné práce
    "4322",  # inštalácia vody, kúrenia a klimatizácie
    "4323",  # ostatné stavebné inštalácie
    "7112",  # inžinierske činnosti a technické poradenstvo
    "8020",  # bezpečnostné systémy vrátane signalizácie
}

TARGET_SOLE_TRADER_ACTIVITY_FRAGMENTS = (
    "elektroinstal",
    "elektromont",
    "elektrotech",
    "elektroenerget",
    "elektrikar",
    "automatizac",
    "slabopr",
    "silnopr",
    "bleskozvod",
    "hromozvod",
    "rozvadz",
    "rozvdz",  # tvar z verejného exportu s poškodenou diakritikou
    "meraniearegul",
)

TARGET_SOLE_TRADER_ELECTRICAL_WORK_FRAGMENTS = (
    "montaz",
    "instal",
    "oprav",
    "udrzb",
    "rekonstruk",
    "projekt",
    "konstru",
    "vyrob",
    "servis",
    "reviz",
    "skus",
    "meran",
)

TARGET_SOLE_TRADER_EXCLUDED_ACTIVITY_FRAGMENTS = (
    "bezzasahudoelektroinstal",
)


def normalize_rpo_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    return " ".join(value.casefold().split())


def is_sro_legal_form(legal_form: Any) -> bool:
    """Vráti True iba pre právnu formu spoločnosti s ručením obmedzeným."""
    return normalize_rpo_label(legal_form) in SRO_LEGAL_FORMS


def is_trade_register(source_register: Any) -> bool:
    """Vráti True pre záznamy zo Živnostenského registra."""
    return normalize_rpo_label(source_register) in TRADE_REGISTER_NAMES


def normalize_rpo_search_text(value: Any) -> str:
    """Normalize RPO labels, including export rows with broken diacritics."""

    if not isinstance(value, str):
        return ""

    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed if character.isalnum())


def matches_target_sole_trader_focus(record: dict[str, Any]) -> bool:
    """Select electrical, construction and closely related sole traders."""

    if not isinstance(record, dict):
        return False

    statistical_codes = record.get("statisticalCodes")
    if not isinstance(statistical_codes, dict):
        statistical_codes = {}
    main_activity = statistical_codes.get("mainActivity")
    if not isinstance(main_activity, dict):
        main_activity = {}

    nace_code = "".join(
        character
        for character in str(main_activity.get("code") or "")
        if character.isdigit()
    )
    if (
        nace_code in TARGET_SOLE_TRADER_NACE_CODES
        or nace_code.startswith(TARGET_SOLE_TRADER_NACE_PREFIXES)
    ):
        return True

    activities = record.get("activities")
    if not isinstance(activities, list):
        return False

    for activity in activities:
        if not isinstance(activity, dict) or activity.get("validTo"):
            continue
        description = normalize_rpo_search_text(
            activity.get("economicActivityDescription")
        )
        if any(
            fragment in description
            for fragment in TARGET_SOLE_TRADER_EXCLUDED_ACTIVITY_FRAGMENTS
        ):
            continue
        if any(
            fragment in description
            for fragment in TARGET_SOLE_TRADER_ACTIVITY_FRAGMENTS
        ):
            return True
        if "elektrick" in description and any(
            fragment in description
            for fragment in TARGET_SOLE_TRADER_ELECTRICAL_WORK_FRAGMENTS
        ):
            return True
        if (
            "signaliz" in description
            and ("poziar" in description or "poiar" in description)
        ):
            return True
        if "zabezpe" in description and "syst" in description:
            return True

    return False


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

    is_supported_record = (
        is_sro_legal_form(normalized.get("legal_form"))
        or is_trade_register(source_register)
    )

    if not is_supported_record:
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
    company.sk_nace_code = normalized.get("sk_nace_code")
    company.sk_nace_name = normalized.get("sk_nace_name")
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
        source_id=str(record.get("id")),
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
    if not isinstance(name, str):
        return ""

    return re.sub(
        r"(?:,\s*)?(?:spol\.\s*s\.?\s*r\.?\s*o\.?|"
        r"s\.?\s*r\.?\s*o\.?|a\.?\s\.?)$",
        "",
        name.strip(),
        flags=re.IGNORECASE,
    ).strip(" ,")

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
    if not isinstance(raw_data, dict):
        return []

    web = raw_data.get("web")

    if not isinstance(web, dict):
        return []

    results = web.get("results")

    if not isinstance(results, list):
        return []

    normalized = []

    for result in results:
        if not isinstance(result, dict):
            continue

        location = result.get("location")
        if not isinstance(location, dict):
            location = {}

        contact = location.get("contact")
        if not isinstance(contact, dict):
            contact = {}

        extra_snippets = result.get("extra_snippets")
        if not isinstance(extra_snippets, list):
            extra_snippets = []

        normalized.append({
            "title": result.get("title"),
            "url": normalize_http_url(result.get("url")),
            "description": result.get("description"),
            "extra_snippets": extra_snippets,

            "location_url": normalize_http_url(location.get("url")),
            "location_email": contact.get("email"),
            "location_phone": contact.get("telephone"),

            "type": result.get("type"),
            "subtype": result.get("subtype"),
        })

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

SOCIAL_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "tiktok.com",
}

SEARCH_EXCLUDED_DOMAINS = {
    "google.com",
    "youtube.com",
}

CONTACT_SCORE_ICO_MATCH = 50
CONTACT_SCORE_NAME_MATCH = 25
CONTACT_SCORE_LOCATION_MATCH = 15
CONTACT_SCORE_EMAIL_DOMAIN_MATCH = 15
CONTACT_SCORE_PHONE_CONFIRMATION = 10
CONTACT_PENALTY_DIRECTORY = 50
CONTACT_PENALTY_UNRELATED = 30
CONTACT_PENALTY_NAME_CONFLICT = 25
CONTACT_PENALTY_SOCIAL_ONLY = 20
VALIDATED_WEBSITE_MINIMUM_CONFIDENCE = 85
UNVERIFIED_CONTACT_MINIMUM_CONFIDENCE = 40

DIRECTORY_DOMAINS = {
    "zoznam.sk",
    "zlatestranky.sk",
    "infoma.sk",
    "slovakregion.sk",
    "kompass.com",
    "dnb.com",
    "foaf.sk",
    "edb.eu",
    "register.peniaze.sk",
    "prever.to",
    "registeruz.sk",
    "valida.sk",
    "transparex.sk",
    "indexpodnikatela.sk",
    "info-bratislava.sk",
    "tvojlekar.sk",
    "e-vuc.sk",
    "zverejnovanie.bratislava.sk",
    "ifirmy.sk",
    "uvostat.sk",
    "skmapy.sk",
    "register.finance.sk",
    "zzz.sk",
    "info-nitra.sk",
    "adresarfiriem.sk",
    "spravodajstvo.sk",
    "b2bhint.com",
    "orlystavebnictva.eu",
    "greatregister.org",
    "stavbahub.sk",
    "autocontact.sk",
    "topdoktor.sk",
    "foursquare.com",
    "nehnutelnosti.sk",
    "tripadvisor.com",
    "superobed.sk",
    "restauracie.sme.sk",
    "menumenu.sk",
    "ekariera.sk",
    "crz.minedu.sk",
    "rejstrik.penize.cz",
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


def is_social_domain(url):
    domain = extract_domain(url)

    if not domain:
        return False

    return any(
        domain == social_domain or domain.endswith(f".{social_domain}")
        for social_domain in SOCIAL_DOMAINS
    )


def is_search_excluded_domain(url):
    domain = extract_domain(url)

    if not domain:
        return True

    return any(
        domain == excluded_domain or domain.endswith(f".{excluded_domain}")
        for excluded_domain in SEARCH_EXCLUDED_DOMAINS
    )


def is_directory_domain(url):
    domain = extract_domain(url)

    if not domain:
        return True

    for directory_domain in DIRECTORY_DOMAINS:
        if domain == directory_domain:
            return True

        if domain.endswith(f".{directory_domain}"):
            return True

    return False


def is_foreign_country_domain(url):
    """Vráti True pre cudzie dvojpísmenové domény, napr. .pt alebo .de."""
    domain = extract_domain(url)
    if not domain or "." not in domain:
        return False

    top_level_domain = domain.rsplit(".", 1)[-1]
    return bool(
        re.fullmatch(r"[a-z]{2}", top_level_domain)
        and top_level_domain != "sk"
    )
def filter_search_results(results):
    if not isinstance(results, list):
        return []

    filtered_results = []

    for result in results:
        if not isinstance(result, dict):
            continue

        url = result.get("url")

        if is_search_excluded_domain(url):
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

    return deduplicate_search_results(all_results)

def deduplicate_search_results(results):
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

def normalize_for_domain(value):
    if not isinstance(value,str):
        value=""
    value=unicodedata.normalize("NFKD",value)
    value="".join(
        char for char in value
        if not unicodedata.combining(char)

    )
    value=value.lower()
    value = re.sub(r"[^a-z0-9]", "", value)
    return value 

def is_value_in_text(text, value):
    normalized_text = normalize_for_domain(text)
    normalized_value = normalize_for_domain(value)

    if not normalized_value:
        return False

    return normalized_value in normalized_text




#+40 ak názov firmy je v doméne
#+20 ak názov firmy je v title
#+10 ak mesto je v description
#+5 ak mesto je v title
def score_search_result(company, result):
    """Ohodnotí zhodu názvu a miesta firmy so search výsledkom."""
    if not isinstance(result, dict):
        return 0

    title = result.get("title") or ""
    description = result.get("description") or ""
    official_name = getattr(company, "official_name", None)
    municipality = getattr(company, "municipality", None)
    street = getattr(company, "street", None)
    company_name = strip_legal_suffix(official_name) if official_name else ""
    normalized_name = normalize_for_domain(company_name)
    normalized_title = normalize_for_domain(title)
    domain = extract_domain(result.get("url"))
    normalized_domain = normalize_for_domain(
        domain.split(".")[0] if domain else ""
    )
    score = 0

    name_similarity = 0.0
    if normalized_name and normalized_title:
        name_similarity = SequenceMatcher(
            None,
            normalized_name,
            normalized_title,
        ).ratio()

    if normalized_name and (
        normalized_name in normalized_title
        or normalized_name in normalized_domain
        or name_similarity >= 0.8
    ):
        score += CONTACT_SCORE_NAME_MATCH
    elif "sro" in normalized_title and normalized_name:
        score -= CONTACT_PENALTY_NAME_CONFLICT

    if (
        is_value_in_text(title, municipality)
        or is_value_in_text(description, municipality)
        or is_value_in_text(title, street)
        or is_value_in_text(description, street)
    ):
        score += CONTACT_SCORE_LOCATION_MATCH

    return score


def build_derived_website_url(company):
    """Vytvorí možnú .sk doménu z obchodného názvu firmy."""
    urls = build_derived_website_urls(company)
    return urls[0] if urls else None


def build_derived_website_urls(company):
    """Vytvorí bezpečné varianty .sk domény vrátane právnej formy."""
    official_name = getattr(company, "official_name", None)
    if not official_name:
        return []

    company_name = strip_legal_suffix(official_name)
    local_company_name = re.sub(
        r"\b(?:slovakia|slovensko)\b",
        "",
        company_name,
        flags=re.IGNORECASE,
    ).strip(" ,.-")
    domain_labels = [
        normalize_for_domain(company_name),
        normalize_for_domain(official_name),
        normalize_for_domain(local_company_name),
    ]
    domain_labels = list(dict.fromkeys(
        label for label in domain_labels if len(label) >= 3
    ))

    return [f"https://{domain_label}.sk" for domain_label in domain_labels]


def extract_visible_page_text(html_content):
    """Odstráni HTML značky a vráti text vhodný na overenie firmy."""
    if not isinstance(html_content, str):
        return ""

    text = re.sub(
        r"<(script|style)[^>]*>.*?</\1>",
        " ",
        html_content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(unescape(text).split())


def fetch_validated_website_result(company, candidate_url):
    """Načíta web a potvrdí, že patrí firme podľa IČO alebo názvu a domény."""
    if (
        not candidate_url
        or is_blocked_domain(candidate_url)
        or is_directory_domain(candidate_url)
        or is_foreign_country_domain(candidate_url)
        or is_social_domain(candidate_url)
    ):
        return None

    try:
        response = safe_http_get(
            candidate_url,
            timeout=15,
            headers={"User-Agent": "LeadAgent-ContactLookup/1.0"},
        )
    except SafeHttpError:
        return None

    if not 200 <= response.status_code < 300:
        return None

    visible_text = extract_visible_page_text(response.text)
    company_ico = str(getattr(company, "ico", "") or "").strip()
    official_name = getattr(company, "official_name", None)
    company_name = strip_legal_suffix(official_name) if official_name else ""
    normalized_name = normalize_for_domain(company_name)
    normalized_official_name = normalize_for_domain(official_name)
    normalized_text = normalize_for_domain(visible_text)
    name_matches = normalized_name and normalized_name in normalized_text
    ico_matches = company_ico and company_ico in visible_text
    domain = extract_domain(response.url)
    domain_label = domain.split(".", 1)[0] if domain else ""
    company_tokens = [
        normalize_for_domain(token)
        for token in re.findall(r"[\wÀ-ž]+", company_name)
    ]
    domain_matches = normalized_name and (
        normalized_name == domain_label
        or (
            (domain or "").endswith(".sk")
            and domain_label in {
                token for token in company_tokens if len(token) >= 4
            }
        )
    )
    exact_legal_domain_match = (
        normalized_official_name
        and normalized_official_name == domain_label
        and (domain or "").endswith(".sk")
    )
    municipality = getattr(company, "municipality", None)
    normalized_municipality = normalize_for_domain(municipality)
    location_matches = (
        normalized_municipality
        and normalized_municipality in normalized_text
    )

    if not ico_matches and not (
        (name_matches and domain_matches)
        or exact_legal_domain_match
    ):
        return None

    if (
        not ico_matches
        and len(normalized_name) <= 5
        and not ((domain or "").endswith(".sk") or location_matches)
    ):
        return None

    title_match = re.search(
        r"<title[^>]*>(.*?)</title>",
        response.text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    title = unescape(title_match.group(1)).strip() if title_match else ""

    return {
        "title": title,
        "url": response.url,
        "description": visible_text[:20000],
        "extra_snippets": [],
        "website_validated": True,
        "website_ico_validated": bool(ico_matches),
    }


def fetch_derived_website_result(company):
    """Overí odvodenú .sk doménu a vráti ju ako search výsledok."""
    for candidate_url in build_derived_website_urls(company):
        result = fetch_validated_website_result(company, candidate_url)
        if result:
            return result

    return None


def validate_company_website_results(company, results, maximum_checks=5):
    """Obsahovo overí najrelevantnejšie Brave výsledky pred výberom kontaktov."""
    if not isinstance(results, list):
        return []

    validated_results = []
    checks_left = maximum_checks

    for result in results:
        if not isinstance(result, dict):
            continue

        if result.get("website_validated"):
            validated_results.append(result)
            continue

        result_url = result.get("url")
        should_check = (
            checks_left > 0
            and not is_blocked_domain(result_url)
            and not is_directory_domain(result_url)
            and not is_foreign_country_domain(result_url)
            and not is_social_domain(result_url)
            and score_search_result(company, result) >= CONTACT_SCORE_NAME_MATCH
        )

        if not should_check:
            validated_results.append(result)
            continue

        checks_left -= 1
        validated_result = fetch_validated_website_result(company, result_url)
        validated_results.append(validated_result or result)

    return validated_results


def has_trusted_website_result(company, results):
    """Určí, či už bol obsahom potvrdený priamy web firmy."""
    return any(
        isinstance(result, dict)
        and result.get("website_validated")
        for result in results
    )


def fetch_company_contact_results(company, delay_seconds=0.5):
    """Vyhľadá a obsahovo overí Brave výsledky pre jednu uloženú firmu."""
    results = []
    queries = build_company_search_queries(company)

    for index, query in enumerate(queries):
        raw_results = fetch_search_results(query)
        results.extend(normalize_search_results(raw_results))

        if delay_seconds and index < len(queries) - 1:
            time.sleep(delay_seconds)

    results = filter_search_results(deduplicate_search_results(results))
    results = validate_company_website_results(company, results)

    if not has_trusted_website_result(company, results):
        derived_result = fetch_derived_website_result(company)

        if derived_result:
            results.append(derived_result)

    return results


def score_contact_candidate(company, candidate, phone_sources):
    """Vráti confidence a rozpis bodov pre jeden zdroj kontaktov."""
    score = score_search_result(company, candidate["result"])
    score_breakdown = []
    result = candidate["result"]
    source_url = candidate["source_url"]
    source_domain = candidate["source_domain"]

    if candidate["ico_match"]:
        score += CONTACT_SCORE_ICO_MATCH
        score_breakdown.append(f"ico_match:+{CONTACT_SCORE_ICO_MATCH}")

    identity_score = score_search_result(company, result)
    if identity_score >= CONTACT_SCORE_NAME_MATCH:
        score_breakdown.append(
            f"company_name_match:+{CONTACT_SCORE_NAME_MATCH}"
        )
    elif identity_score <= -CONTACT_PENALTY_NAME_CONFLICT:
        score_breakdown.append(
            f"company_name_conflict:-{CONTACT_PENALTY_NAME_CONFLICT}"
        )

    if (
        identity_score % CONTACT_SCORE_NAME_MATCH
        >= CONTACT_SCORE_LOCATION_MATCH
        or identity_score >= (
            CONTACT_SCORE_NAME_MATCH + CONTACT_SCORE_LOCATION_MATCH
        )
    ):
        score_breakdown.append(
            f"location_or_address_match:+{CONTACT_SCORE_LOCATION_MATCH}"
        )

    website_domains = set(candidate["website_domains"])
    if source_domain:
        website_domains.add(source_domain)

    if any(
        email.rsplit("@", 1)[-1] in website_domains
        for email in candidate["emails"]
        if "@" in email
    ):
        score += CONTACT_SCORE_EMAIL_DOMAIN_MATCH
        score_breakdown.append(
            f"email_domain_matches_website:+{CONTACT_SCORE_EMAIL_DOMAIN_MATCH}"
        )

    if any(
        len(phone_sources.get(normalize_phone(phone), set())) > 1
        for phone in candidate["phones"]
        if normalize_phone(phone)
    ):
        score += CONTACT_SCORE_PHONE_CONFIRMATION
        score_breakdown.append(
            f"phone_in_multiple_sources:+{CONTACT_SCORE_PHONE_CONFIRMATION}"
        )

    if is_directory_domain(source_url):
        score -= CONTACT_PENALTY_DIRECTORY
        score_breakdown.append(
            f"directory_or_register:-{CONTACT_PENALTY_DIRECTORY}"
        )
    elif (
        not candidate["ico_match"]
        and identity_score == 0
        and not is_social_domain(source_url)
    ):
        score -= CONTACT_PENALTY_UNRELATED
        score_breakdown.append(
            f"unrelated_domain:-{CONTACT_PENALTY_UNRELATED}"
        )

    if (
        is_social_domain(source_url)
        and not candidate["ico_match"]
        and identity_score < 40
    ):
        score -= CONTACT_PENALTY_SOCIAL_ONLY
        score_breakdown.append(
            f"social_without_additional_evidence:-{CONTACT_PENALTY_SOCIAL_ONLY}"
        )

    return max(0, min(score, 100)), score_breakdown
    
def choose_best_website(company,results,minimum_score=40):
    if not isinstance(results,list):
        return None
    best_result = None
    best_score=0

    for result in results:
        score=score_search_result(company,result)
        if score > best_score:
            best_result=result
    if not best_result or best_score < minimum_score:
        return None 
    return {
        "title": best_result.get("title"),
        "url": best_result.get("url"),
        "description": best_result.get("description"),
        "domain": extract_domain(best_result.get("url")),
        "score": best_score,
    }

def find_company_website(company):
    queries = build_company_search_queries(company)
    all_results = []

    for query in queries:
        results = search_company_web(query)
        all_results.extend(results)

    unique_results = deduplicate_search_results(all_results)

    return choose_best_website(company, unique_results)

def debug_search_results(response):
    results = response.get("web", {}).get("results", [])
    print(json.dumps(response, indent=2, ensure_ascii=False))

    print(f"Počet výsledkov: {len(results)}")

    for i, result in enumerate(results, start=1):
        print(f"\n=== {i} ===")
        print("Title:", result.get("title"))
        print("URL:", result.get("url"))
        print("Description:", result.get("description"))



{
    "website": "http://www.elektroinstalaciepoprad.sk",
    "emails": [
        "elektroinstalaciepoprad@gmail.com",
        "rudolfgorel@gmail.com"
    ],
    "phones": [
        "0903 628 912",
        "0948 132 867"
    ],
    "ico": "48061999",
    "source_url": "https://www.zoznam.sk/firma/3172905/Elektroinstalacie-Poprad-Poprad",
    "source_type": "search_result_description"
}


#-----------------------------------------------------------------Dokoncit-------------------------------
def extract_contacts_from_search_result(result):
    if not isinstance(result, dict):
        return {
            "websites": [],
            "emails": [],
            "phones": [],
            "icos": [],
        }

    parts = [
        result.get("title"),
        result.get("description"),
    ]

    extra_snippets = result.get("extra_snippets")

    if isinstance(extra_snippets, list):
        parts.extend(extra_snippets)

    text = " ".join(
        str(part).strip()
        for part in parts
        if part
    )
   


    websites = extract_websites(text)
    emails = extract_emails(text)
    phones = extract_phones(text)
    icos = extract_icos(text)

    location_url = result.get("location_url")
    location_email = result.get("location_email")
    location_phone = result.get("location_phone")

    if location_url:
        websites.append(location_url)

    if location_email:
        emails.append(location_email.lower().strip())

    if location_phone:
        phone = normalize_phone(location_phone)

        if phone:
            phones.append(phone)

    return {
        "websites": list(dict.fromkeys(websites)),
        "emails": list(dict.fromkeys(emails)),
        "phones": list(dict.fromkeys(phones)),
        "icos": list(dict.fromkeys(icos)),
    }

#--------------------------------------------------Dokoncit----------------------------

def extract_emails(text):
    if not isinstance(text,str):
        return []
    pattern = (
        r"[A-Za-z0-9._%+-]+"
        r"@[A-Za-z0-9.-]+"
        r"\.[A-Za-z]{2,}"
    )

    found=re.findall(pattern,text)

    seen=set()
    result=[]
    for email in found:
        email= email.lower().strip()
        if email in seen:
            continue
        seen.add(email)
        result.append(email)
    return result

def extract_websites(text):
    if not isinstance(text, str):
        return []

    pattern = r"https?://[^\s<>\"']+"

    found = re.findall(pattern, text, flags=re.IGNORECASE)

    seen = set()
    result = []

    for url in found:
        url = url.rstrip(".,;:!?)]}").strip()

        if url in seen:
            continue

        seen.add(url)
        result.append(url)

    return result

def normalize_phone(phone):
    if not isinstance(phone, str):
        return None

    phone = phone.strip()

    has_plus = phone.startswith("+")
    digits = re.sub(r"\D", "", phone)

    if not digits:
        return None

    if digits.startswith("00421"):
        return f"+{digits[2:]}"

    if digits.startswith("421"):
        return f"+{digits}"

    if digits.startswith("0") and len(digits) in (9, 10):
        return f"+421{digits[1:]}"

    return f"+{digits}" if has_plus else digits

def extract_phones(text):
    if not isinstance(text, str):
        return []

    pattern = (
        r"(?<!\d)"
        r"(?:\+421|00421|0)"
        r"(?:[\s\-()]?\d){8,9}"
        r"(?!\d)"
    )

    found = re.findall(pattern, text)

    seen = set()
    result = []

    for phone in found:
        phone = normalize_phone(phone)

        if not phone or phone in seen:
            continue

        seen.add(phone)
        result.append(phone)

    return result
def extract_icos(text):
    if not isinstance(text, str):
        return []

    pattern = r"\bIČO\s*:?\s*(\d{8})\b"

    found = re.findall(
        pattern,
        text,
        flags=re.IGNORECASE,
    )

    return list(dict.fromkeys(found))


def choose_verified_website(company, results):
    for result in results:
        extracted = extract_contacts_from_search_result(result)

        if extracted["ico"] != company.ico:
            continue

        domain = extract_domain(result.get("url"))

        if not domain:
            continue

        if is_blocked_domain(result.get("url")):
            continue

        return {
            "domain": domain,
            "url": f"https://{domain}",
            "source_url": result.get("url"),
            "verification_method": "ico_match",
        }

    return None


def aggregate_company_contacts(company, results):
    if not isinstance(results, list):
        return {
            "verified_domains": [],
            "websites": [],
            "emails": [],
            "phones": [],
            "evidence": [],
            "possible_contacts": {
                "websites": [],
                "emails": [],
                "phones": [],
            },
        }

    company_ico = str(company.ico).strip() if company.ico else None
    candidates = []

    for result in results:
        if not isinstance(result, dict):
            continue

        extracted = extract_contacts_from_search_result(result)

        source_url = result.get("url")
        source_domain = extract_domain(source_url)

        extracted_icos = extracted.get("icos", [])
        websites = extracted.get("websites", [])
        emails = extracted.get("emails", [])
        phones = extracted.get("phones", [])

        ico_match = bool(
            company_ico
            and company_ico in extracted_icos
        )

        website_domains = []

        for website in websites:
            domain = extract_domain(website)

            if (
                domain
                and not is_blocked_domain(website)
                and not is_directory_domain(website)
                and not is_foreign_country_domain(website)
            ):
                website_domains.append(domain)

        website_domains = list(dict.fromkeys(website_domains))

        candidates.append({
            "result": result,
            "source_url": source_url,
            "source_domain": source_domain,
            "website_domains": website_domains,
            "websites": websites,
            "emails": emails,
            "phones": phones,
            "icos": extracted_icos,
            "ico_match": ico_match,
            "website_validated": bool(result.get("website_validated")),
            "website_ico_validated": bool(result.get("website_ico_validated")),
        })

    phone_sources = {}

    for candidate in candidates:
        source_key = candidate["source_domain"] or candidate["source_url"]

        if not source_key:
            continue

        for phone in candidate["phones"]:
            normalized_phone = normalize_phone(phone)

            if normalized_phone:
                phone_sources.setdefault(normalized_phone, set()).add(source_key)

    for candidate in candidates:
        source_score, score_breakdown = score_contact_candidate(
            company,
            candidate,
            phone_sources,
        )
        source_url = candidate["source_url"]
        candidate["source_score"] = source_score
        candidate["score_breakdown"] = score_breakdown
        candidate["source_is_company_website"] = bool(
            candidate["source_domain"]
            and source_url
            and not is_blocked_domain(source_url)
            and not is_directory_domain(source_url)
            and not is_foreign_country_domain(source_url)
            and candidate["website_validated"]
        )
        candidate["social_profile_match"] = bool(
            is_social_domain(source_url)
            and source_score >= 40
        )

    verified_domains = set()
    verified_email_domains = {}

    for candidate in candidates:
        if (
            candidate["ico_match"]
            and not is_directory_domain(candidate["source_url"])
        ):
            verified_domains.update(candidate["website_domains"])

        if candidate["source_is_company_website"]:
            verified_domains.add(candidate["source_domain"])

        if candidate["ico_match"]:
            for email in candidate["emails"]:
                normalized_email = normalize_contact_value("email", email)
                if not normalized_email or "@" not in normalized_email:
                    continue

                email_domain = normalized_email.rsplit("@", 1)[-1]
                if (
                    not is_blocked_domain(email_domain)
                    and not is_directory_domain(email_domain)
                    and not is_foreign_country_domain(email_domain)
                ):
                    verified_email_domains.setdefault(
                        email_domain,
                        candidate["source_url"],
                    )

    websites = []
    emails = []
    phones = []
    evidence = []
    possible_contacts = {
        "websites": [],
        "emails": [],
        "phones": [],
    }

    seen_websites = set()
    seen_emails = set()
    seen_phones = set()
    seen_possible_websites = set()
    seen_possible_emails = set()
    seen_possible_phones = set()

    for email_domain, source_url in verified_email_domains.items():
        website_url = f"https://{email_domain}"
        seen_websites.add(website_url)
        websites.append({
            "value": website_url,
            "domain": email_domain,
            "source_url": source_url,
            "confidence": 95,
            "reason": "ico_verified_email_domain",
            "ico_validated": True,
        })

    for candidate in candidates:
        matching_domains = (
            set(candidate["website_domains"])
            & verified_domains
        )
        source_domain_verified = (
            candidate["source_domain"] in verified_domains
        )

        belongs_to_company = candidate["source_is_company_website"]

        if not belongs_to_company:
            if (
                candidate["ico_match"]
                and candidate["source_score"]
                >= UNVERIFIED_CONTACT_MINIMUM_CONFIDENCE
            ):
                reason = "ico_match_unverified_source"
            elif candidate["social_profile_match"]:
                reason = "social_profile_name_and_municipality_match"
            elif (
                candidate["source_score"]
                >= UNVERIFIED_CONTACT_MINIMUM_CONFIDENCE
                and not is_blocked_domain(candidate["source_url"])
            ):
                reason = "name_location_unverified_source"
            else:
                continue

            source_url = candidate["source_url"]

            for website in candidate["websites"]:
                domain = extract_domain(website)

                if (
                    not domain
                    or is_blocked_domain(website)
                    or is_directory_domain(website)
                    or is_foreign_country_domain(website)
                    or website in seen_possible_websites
                ):
                    continue

                seen_possible_websites.add(website)
                possible_contacts["websites"].append({
                    "value": website.strip(),
                    "domain": domain,
                    "source_url": source_url,
                    "confidence": candidate["source_score"],
                    "reason": reason,
                })

            for email in candidate["emails"]:
                normalized_email = normalize_contact_value("email", email)

                if not normalized_email or normalized_email in seen_possible_emails:
                    continue

                seen_possible_emails.add(normalized_email)
                possible_contacts["emails"].append({
                    "value": normalized_email,
                    "source_url": source_url,
                    "confidence": candidate["source_score"],
                    "reason": reason,
                })

            for phone in candidate["phones"]:
                normalized_phone = normalize_phone(phone)

                if not normalized_phone or normalized_phone in seen_possible_phones:
                    continue

                seen_possible_phones.add(normalized_phone)
                possible_contacts["phones"].append({
                    "value": normalized_phone,
                    "source_url": source_url,
                    "confidence": candidate["source_score"],
                    "reason": reason,
                })

            evidence.append({
                "source_url": source_url,
                "ico_match": candidate["ico_match"],
                "source_score": candidate["source_score"],
                "score_breakdown": candidate["score_breakdown"],
                "matching_domains": [],
                "reason": reason,
            })
            continue

        if candidate["website_validated"]:
            reason = "validated_company_website"
            base_confidence = max(
                candidate["source_score"],
                VALIDATED_WEBSITE_MINIMUM_CONFIDENCE,
            )
        elif candidate["ico_match"]:
            reason = "ico_match"
            base_confidence = candidate["source_score"]
        elif source_domain_verified:
            reason = "verified_source_domain"
            base_confidence = candidate["source_score"]
        else:
            reason = "verified_domain_match"
            base_confidence = candidate["source_score"]

        source_url = candidate["source_url"]

        if source_domain_verified and source_url:
            normalized_source_url = source_url.strip()

            if normalized_source_url not in seen_websites:
                seen_websites.add(normalized_source_url)
                websites.append({
                    "value": normalized_source_url,
                    "domain": candidate["source_domain"],
                    "source_url": source_url,
                    "confidence": base_confidence,
                    "reason": reason,
                    "ico_validated": candidate["website_ico_validated"],
                })

        for website in candidate["websites"]:
            domain = extract_domain(website)

            if not domain or domain not in verified_domains:
                continue

            normalized_website = website.strip()

            if normalized_website in seen_websites:
                continue

            seen_websites.add(normalized_website)

            websites.append({
                "value": normalized_website,
                "domain": domain,
                "source_url": source_url,
                "confidence": base_confidence,
                "reason": reason,
                "ico_validated": candidate["website_ico_validated"],
            })

        for email in candidate["emails"]:
            normalized_email = normalize_contact_value(
                "email",
                email,
            )

            if not normalized_email:
                continue

            email_domain = normalized_email.rsplit("@", 1)[-1]

            if not candidate["ico_match"] and email_domain not in verified_domains:
                continue

            if normalized_email in seen_emails:
                continue

            seen_emails.add(normalized_email)

            emails.append({
                "value": normalized_email,
                "source_url": source_url,
                "confidence": base_confidence,
                "reason": reason,
                "ico_validated": candidate["website_ico_validated"],
            })

        for phone in candidate["phones"]:
            if not candidate["ico_match"] and not source_domain_verified:
                continue

            normalized_phone = normalize_phone(phone)

            if not normalized_phone:
                continue

            if normalized_phone in seen_phones:
                continue

            seen_phones.add(normalized_phone)

            phones.append({
                "value": normalized_phone,
                "source_url": source_url,
                "confidence": base_confidence,
                "reason": reason,
                "ico_validated": candidate["website_ico_validated"],
            })

        evidence.append({
            "source_url": source_url,
            "ico_match": candidate["ico_match"],
            "source_score": candidate["source_score"],
            "score_breakdown": candidate["score_breakdown"],
            "matching_domains": list(matching_domains),
            "reason": reason,
        })

    return {
        "verified_domains": sorted(verified_domains),
        "websites": websites,
        "emails": emails,
        "phones": phones,
        "evidence": evidence,
        "possible_contacts": possible_contacts,
    }


CONTACT_SELECTION_RULES = {
    "websites": {
        "contact_type": "website",
        "minimum_confidence": 70,
    },
    "emails": {
        "contact_type": "email",
        "minimum_confidence": 80,
    },
    "phones": {
        "contact_type": "phone",
        "minimum_confidence": 80,
    },
}

CONTACT_REASON_PRIORITY = {
    "validated_company_website": 4,
    "ico_match": 3,
    "ico_verified_email_domain": 3,
    "verified_source_domain": 2,
    "verified_domain_match": 1,
    "ico_match_unverified_source": 1,
    "social_profile_name_and_municipality_match": 0,
    "name_location_unverified_source": 0,
}


def select_best_company_contacts(aggregated, include_candidates=False):
    if not isinstance(aggregated, dict):
        return {}

    selected = {}

    for collection_name, rule in CONTACT_SELECTION_RULES.items():
        contacts = aggregated.get(collection_name, [])

        if not isinstance(contacts, list):
            continue

        contacts = list(contacts)

        if include_candidates:
            possible_contacts = aggregated.get("possible_contacts", {})
            if isinstance(possible_contacts, dict):
                candidate_contacts = possible_contacts.get(collection_name, [])

                if isinstance(candidate_contacts, list):
                    contacts.extend(candidate_contacts)

        eligible = []

        for contact in contacts:
            if not isinstance(contact, dict):
                continue

            value = contact.get("value")
            confidence = contact.get("confidence")

            if not isinstance(value, str) or not value.strip():
                continue

            if not isinstance(confidence, (int, float)):
                continue

            minimum_confidence = rule["minimum_confidence"]
            if contact.get("reason") == "validated_company_website":
                minimum_confidence = VALIDATED_WEBSITE_MINIMUM_CONFIDENCE
            elif contact.get("reason") in {
                "ico_match_unverified_source",
                "social_profile_name_and_municipality_match",
                "name_location_unverified_source",
            }:
                minimum_confidence = UNVERIFIED_CONTACT_MINIMUM_CONFIDENCE

            if confidence < minimum_confidence:
                continue

            source_url = contact.get("source_url")
            is_unverified_candidate = contact.get("reason") in {
                "ico_match_unverified_source",
                "social_profile_name_and_municipality_match",
                "name_location_unverified_source",
            }
            if (
                source_url
                and is_directory_domain(source_url)
                and not is_unverified_candidate
            ):
                continue

            if (
                collection_name == "websites"
                and (
                    is_directory_domain(value)
                    or is_foreign_country_domain(value)
                )
            ):
                continue

            eligible.append(contact)

        if not eligible:
            continue

        selected[rule["contact_type"]] = max(
            eligible,
            key=lambda contact: (
                contact["confidence"],
                CONTACT_REASON_PRIORITY.get(contact.get("reason"), 0),
                contact.get("value", ""),
            ),
        )

    return selected


def save_best_company_contacts(company, aggregated, include_candidates=True):
    selected_contacts = select_best_company_contacts(
        aggregated,
        include_candidates=include_candidates,
    )

    if not selected_contacts:
        return []

    existing_contacts = list(company.contacts)
    saved_contacts = []

    for contact_type, selected_contact in selected_contacts.items():
        value = selected_contact["value"].strip()
        if contact_type == "website":
            value = normalize_http_url(value)
            if not value:
                continue
        contact = next(
            (
                existing_contact
                for existing_contact in existing_contacts
                if (
                    existing_contact.contact_type == contact_type
                    and existing_contact.value == value
                )
            ),
            None,
        )

        is_verified = bool(selected_contact.get("ico_validated")) or (
            selected_contact.get("reason") in {
                "ico_match",
                "ico_verified_email_domain",
            }
        )
        has_verified_contact = any(
            existing_contact.contact_type == contact_type
            and existing_contact.is_verified
            for existing_contact in existing_contacts
        )

        if contact is None:
            contact = CompanyContact(
                company=company,
                contact_type=contact_type,
                value=value,
                source_type=(
                    "brave_validated"
                    if is_verified
                    else "brave_candidate"
                ),
            )
            db.session.add(contact)
            existing_contacts.append(contact)

        if contact.source_type in {
            "brave_search",
            "brave_candidate",
            "brave_validated",
        }:
            contact.source_url = normalize_http_url(
                selected_contact.get("source_url")
            )
            contact.label = selected_contact.get("reason")
            contact.confidence_score = selected_contact["confidence"]
            contact.source_type = (
                "brave_validated"
                if is_verified
                else "brave_candidate"
            )

        contact.is_verified = contact.is_verified or is_verified
        if is_verified or not has_verified_contact:
            for existing_contact in existing_contacts:
                if existing_contact.contact_type == contact_type:
                    existing_contact.is_primary = False
            contact.is_primary = True

        if is_verified:
            contact.last_verified_at = utcnow()

        saved_contacts.append(contact)

    return saved_contacts


def mark_directory_contacts_unverified():
    """Označí staré Brave kontakty z katalógov ako neoverené.

    Záznamy nemažeme, aby zostali dostupné na manuálnu kontrolu.
    """
    updated_contacts = 0

    for contact in CompanyContact.query.all():
        if contact.source_type not in {
            "brave_search",
            "brave_candidate",
            "brave_validated",
        }:
            continue

        is_directory_website = (
            contact.contact_type == "website"
            and (
                is_directory_domain(contact.value)
                or is_foreign_country_domain(contact.value)
            )
        )

        source_is_directory = bool(
            contact.source_url and is_directory_domain(contact.source_url)
        )

        if not source_is_directory and not is_directory_website:
            continue

        contact.is_verified = False
        contact.is_primary = False
        contact.label = "directory_source_unverified"
        contact.source_type = "brave_candidate"
        updated_contacts += 1

    for contact in CompanyContact.query.filter_by(
        source_type="brave_validated",
    ).all():
        if contact.confidence_score == 100:
            continue

        contact.is_verified = False
        contact.label = "domain_match_unverified"
        contact.source_type = "brave_candidate"
        updated_contacts += 1

    return updated_contacts


def companies_for_contact_enrichment(include_existing: bool) -> list[Company]:
    """Vyberie firmy pre prvú alebo vynútenú opakovanú kontrolu kontaktov."""
    query = Company.query.order_by(Company.created_at.desc())

    if include_existing:
        return query.all()

    return query.filter(
        Company.contacts_checked_at.is_(None),
        ~Company.contacts.any(),
    ).all()


def enrich_company_contacts(
    max_companies=None,
    delay_seconds=0.5,
    include_existing=False,
    companies: list[Company] | None = None,
):
    """Doplní kontakty z Brave pre firmy uložené v databáze.

    Overené kontakty sa označia ako verified. Pri presnej zhode IČO sa
    uloží aj kandidátny e-mail alebo telefón s označením unverified,
    aby zostal dostupný na následnú manuálnu kontrolu.
    """
    if companies is None:
        companies = companies_for_contact_enrichment(include_existing)

        if max_companies is not None:
            companies = companies[:max_companies]

    summary = {
        "processed_companies": 0,
        "companies_with_contacts": 0,
        "companies_without_contacts": 0,
        "saved_contacts": 0,
        "reclassified_directory_contacts": mark_directory_contacts_unverified(),
        "errors": [],
    }

    db.session.commit()
    count = 1
    for company in companies:
        try:
            
            results = fetch_company_contact_results(company, delay_seconds)
            aggregated = aggregate_company_contacts(company, results)
            saved_contacts = save_best_company_contacts(
                company,
                aggregated,
                include_candidates=True,
            )
            company.contacts_checked_at = utcnow()
            print(f"{count}. Spracovaná firma: {company.official_name}: {len(saved_contacts)} kontaktov")
            count += 1
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception(
                "Nepodarilo sa doplniť kontakty pre IČO %s",
                company.ico,
            )
            summary["errors"].append({
                "ico": company.ico,
                "error": "Načítanie kontaktov zlyhalo. Podrobnosti sú v serverovom logu.",
            })
            continue

        summary["processed_companies"] += 1
        summary["saved_contacts"] += len(saved_contacts)

        if saved_contacts:
            summary["companies_with_contacts"] += 1
        else:
            summary["companies_without_contacts"] += 1

    return summary

        




        
    

















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

    return normalize_rpo_api_url(url)


def normalize_rpo_api_url(value: str) -> str:
    """Allow only HTTPS URLs on the configured RPO API origin."""
    try:
        candidate = urljoin(f"{RPO_BASE_URL.rstrip('/')}/", str(value or "").strip())
        parsed = urlsplit(candidate)
        base = urlsplit(RPO_BASE_URL)
        candidate_port = parsed.port or 443
        base_port = base.port or 443
    except (TypeError, ValueError) as exc:
        raise RpoSyncError("RPO API vrátilo neplatnú URL.") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or not base.hostname
        or parsed.hostname.casefold() != base.hostname.casefold()
        or candidate_port != base_port
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RpoSyncError("RPO API sa pokúsilo presmerovať mimo povoleného originu.")
    return candidate


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
    current_url = normalize_rpo_api_url(url)
    response = None
    transient_errors = (
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )

    for request_attempt in range(3):
        try:
            for redirect_number in range(4):
                response = session.get(
                    current_url,
                    timeout=(10, 60),
                    allow_redirects=False,
                )
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("Location")
                if not location or redirect_number == 3:
                    raise RpoSyncError(
                        "RPO API vrátilo neplatné alebo príliš dlhé presmerovanie."
                    )
                current_url = normalize_rpo_api_url(
                    urljoin(current_url, location)
                )
            break
        except transient_errors as exc:
            if request_attempt == 2:
                raise RpoSyncError(
                    "RPO API opakovane prerušilo spojenie počas načítania stránky."
                ) from exc

            wait_seconds = 2 ** request_attempt
            logger.warning(
                "RPO request interrupted; retry=%s wait=%ss url=%s error=%s",
                request_attempt + 1,
                wait_seconds,
                current_url,
                type(exc).__name__,
            )
            time.sleep(wait_seconds)

    if response.status_code == 429:
        raise RpoSyncError(
            "RPO API odmietlo požiadavku pre prekročenie limitu."
        )

    if not response.ok:
        raise RpoSyncError(f"RPO API vrátilo HTTP {response.status_code}.")

    return response


def build_rpo_export_url(batch_date: str, file_number: int) -> str:
    """Build a fixed-origin URL for one official monthly RPO export file."""

    try:
        normalized_date = date.fromisoformat(batch_date).isoformat()
    except (TypeError, ValueError) as exc:
        raise RpoSyncError("Neplatný dátum RPO exportu.") from exc

    if not isinstance(file_number, int) or not 1 <= file_number <= 999:
        raise RpoSyncError("Neplatné číslo súboru RPO exportu.")

    return (
        f"{RPO_EXPORT_BASE_URL}/"
        f"init_{normalized_date}_{file_number:03d}.json.gz"
    )


def iter_rpo_export_records(response: Response):
    """Stream records from one official line-delimited JSON gzip export."""

    response.raw.decode_content = False
    with gzip.GzipFile(fileobj=response.raw) as compressed:
        with io.TextIOWrapper(compressed, encoding="utf-8") as source:
            header = source.readline().strip()
            if not header.startswith('{"exportDate"') or '"results":[' not in header:
                raise RpoSyncError("RPO export má neznámy formát hlavičky.")

            for line in source:
                payload = line.strip()
                if not payload or payload == "]}":
                    continue

                payload = payload.lstrip(",")
                try:
                    record = json.loads(payload)
                except ValueError as exc:
                    raise RpoSyncError(
                        "RPO export obsahuje neplatný JSON záznam."
                    ) from exc

                if not isinstance(record, dict):
                    raise RpoSyncError("RPO export obsahuje neplatný záznam.")

                yield record


def import_rpo_sole_traders_export(
    *,
    batch_date: str,
    file_number: int,
    max_records: int = 10_000,
    commit_every: int = 100,
) -> dict[str, Any]:
    """Import target-sector active sole traders from one RPO export file."""

    if max_records < 1:
        raise RpoSyncError("Limit importu musí byť aspoň 1.")
    if commit_every < 1:
        raise RpoSyncError("Interval ukladania musí byť aspoň 1.")

    url = build_rpo_export_url(batch_date, file_number)
    session = build_http_session()
    response = None
    scanned = 0
    active_sole_traders_scanned = 0
    skipped_outside_target_focus = 0
    selected = 0
    upserted = 0

    try:
        response = session.get(
            url,
            timeout=(10, 180),
            allow_redirects=False,
            stream=True,
        )
        if response.status_code != 200:
            raise RpoSyncError(
                f"RPO export vrátil HTTP {response.status_code}."
            )

        for export_record in iter_rpo_export_records(response):
            scanned += 1
            if not is_trade_register(
                extract_source_register_name({"data": export_record})
            ):
                continue
            if export_record.get("termination"):
                continue

            active_sole_traders_scanned += 1
            if not matches_target_sole_trader_focus(export_record):
                skipped_outside_target_focus += 1
                continue

            selected += 1
            wrapped_record = {
                "id": export_record.get("id"),
                "data": export_record,
            }
            if upsert_rpo_record(wrapped_record) is not None:
                upserted += 1

            if selected % commit_every == 0:
                db.session.commit()

            if selected >= max_records:
                break

        db.session.commit()
        return {
            "status": "success",
            "batch_date": batch_date,
            "file_number": file_number,
            "scanned": scanned,
            "active_sole_traders_scanned": active_sole_traders_scanned,
            "skipped_outside_target_focus": skipped_outside_target_focus,
            "selected_active_sole_traders": selected,
            "upserted": upserted,
            "target_nace_prefixes": TARGET_SOLE_TRADER_NACE_PREFIXES,
            "target_nace_codes": sorted(TARGET_SOLE_TRADER_NACE_CODES),
            "source_url": url,
        }
    except Exception:
        db.session.rollback()
        raise
    finally:
        if response is not None:
            response.close()
        session.close()


def build_rpo_export_checkpoint(
    *,
    batch_date: str,
    first_file: int,
    last_file: int,
    file_number: int,
    record_offset: int,
    completed: bool = False,
) -> str:
    return json.dumps(
        {
            "batch_date": batch_date,
            "filter_version": RPO_SOLE_TRADER_FILTER_VERSION,
            "first_file": first_file,
            "last_file": last_file,
            "file_number": file_number,
            "record_offset": record_offset,
            "completed": completed,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_rpo_export_checkpoint(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        checkpoint = json.loads(value)
    except ValueError as exc:
        raise RpoSyncError("Checkpoint RPO exportu je poškodený.") from exc

    if not isinstance(checkpoint, dict):
        raise RpoSyncError("Checkpoint RPO exportu má neplatný formát.")

    required = {
        "batch_date",
        "filter_version",
        "first_file",
        "last_file",
        "file_number",
        "record_offset",
        "completed",
    }
    if not required.issubset(checkpoint):
        raise RpoSyncError("Checkpoint RPO exportu je neúplný.")

    return checkpoint


def get_or_create_rpo_export_sync_state() -> SyncState:
    state = SyncState.query.filter_by(
        name=RPO_SOLE_TRADER_EXPORT_SYNC_NAME
    ).one_or_none()
    if state is None:
        state = SyncState(
            name=RPO_SOLE_TRADER_EXPORT_SYNC_NAME,
            status="idle",
            processed_records=0,
            fetched_records=0,
            skipped_records=0,
        )
        db.session.add(state)
        db.session.commit()
    return state


def import_rpo_sole_traders_batch(
    *,
    batch_date: str,
    first_file: int = 1,
    last_file: int = 23,
    commit_every: int = 100,
    max_records: int | None = None,
    resume: bool = True,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Import a resumable RPO batch and checkpoint every committed chunk."""

    try:
        normalized_date = date.fromisoformat(batch_date).isoformat()
    except (TypeError, ValueError) as exc:
        raise RpoSyncError("Neplatný dátum RPO exportu.") from exc
    if not 1 <= first_file <= last_file <= 999:
        raise RpoSyncError("Neplatný rozsah súborov RPO exportu.")
    if commit_every < 1:
        raise RpoSyncError("Interval ukladania musí byť aspoň 1.")
    if max_records is not None and max_records < 1:
        raise RpoSyncError("Limit importu musí byť aspoň 1.")

    stop_requested = should_stop or (lambda: False)
    state = get_or_create_rpo_export_sync_state()
    checkpoint = parse_rpo_export_checkpoint(state.next_url)

    expected_checkpoint = {
        "batch_date": normalized_date,
        "filter_version": RPO_SOLE_TRADER_FILTER_VERSION,
        "first_file": first_file,
        "last_file": last_file,
    }
    if resume and checkpoint:
        actual_checkpoint = {
            key: checkpoint.get(key)
            for key in expected_checkpoint
        }
        if actual_checkpoint != expected_checkpoint:
            raise RpoSyncError(
                "Uložený checkpoint patrí inej dávke alebo rozsahu. "
                "Použite --restart."
            )
        if checkpoint.get("completed"):
            return {
                "status": "success",
                "already_completed": True,
                "batch_date": normalized_date,
                "first_file": first_file,
                "last_file": last_file,
                "fetched": state.fetched_records,
                "imported": state.processed_records,
                "skipped": state.skipped_records,
                "checkpoint": checkpoint,
            }
        current_file = int(checkpoint["file_number"])
        record_offset = int(checkpoint["record_offset"])
    else:
        current_file = first_file
        record_offset = 0
        state.processed_records = 0
        state.fetched_records = 0
        state.skipped_records = 0
        state.sync_started_at = utcnow()

    state.status = "running"
    state.last_error = None
    state.next_url = build_rpo_export_checkpoint(
        batch_date=normalized_date,
        first_file=first_file,
        last_file=last_file,
        file_number=current_file,
        record_offset=record_offset,
    )
    db.session.commit()

    if (
        max_records is not None
        and state.processed_records >= max_records
    ):
        state.status = "partial"
        db.session.commit()
        return {
            "status": "partial",
            "batch_date": normalized_date,
            "first_file": first_file,
            "last_file": last_file,
            "max_records": max_records,
            "fetched": state.fetched_records,
            "imported": state.processed_records,
            "skipped": state.skipped_records,
            "checkpoint": parse_rpo_export_checkpoint(state.next_url),
        }

    session = build_http_session()
    response = None
    records_since_commit = 0

    def save_checkpoint(
        *,
        status: str,
        file_number: int,
        offset: int,
        completed: bool = False,
    ) -> dict[str, Any]:
        state.status = status
        state.next_url = build_rpo_export_checkpoint(
            batch_date=normalized_date,
            first_file=first_file,
            last_file=last_file,
            file_number=file_number,
            record_offset=offset,
            completed=completed,
        )
        if completed:
            state.last_successful_sync_at = utcnow()
        db.session.commit()
        return {
            "status": status,
            "batch_date": normalized_date,
            "first_file": first_file,
            "last_file": last_file,
            "max_records": max_records,
            "fetched": state.fetched_records,
            "imported": state.processed_records,
            "skipped": state.skipped_records,
            "checkpoint": parse_rpo_export_checkpoint(state.next_url),
        }

    try:
        for file_number in range(current_file, last_file + 1):
            offset = record_offset if file_number == current_file else 0
            if stop_requested():
                return save_checkpoint(
                    status="stopped",
                    file_number=file_number,
                    offset=offset,
                )

            response = session.get(
                build_rpo_export_url(normalized_date, file_number),
                timeout=(10, 180),
                allow_redirects=False,
                stream=True,
            )
            if response.status_code != 200:
                raise RpoSyncError(
                    f"RPO export vrátil HTTP {response.status_code}."
                )

            for position, export_record in enumerate(
                iter_rpo_export_records(response),
                start=1,
            ):
                if position <= offset:
                    continue

                state.fetched_records += 1
                is_target = (
                    is_trade_register(
                        extract_source_register_name({"data": export_record})
                    )
                    and not export_record.get("termination")
                    and matches_target_sole_trader_focus(export_record)
                )
                if is_target:
                    wrapped_record = {
                        "id": export_record.get("id"),
                        "data": export_record,
                    }
                    if upsert_rpo_record(wrapped_record) is not None:
                        state.processed_records += 1
                    else:
                        state.skipped_records += 1
                else:
                    state.skipped_records += 1

                offset = position
                records_since_commit += 1
                stop_now = stop_requested()
                limit_reached = (
                    max_records is not None
                    and state.processed_records >= max_records
                )
                if (
                    records_since_commit >= commit_every
                    or stop_now
                    or limit_reached
                ):
                    status = "running"
                    if stop_now:
                        status = "stopped"
                    elif limit_reached:
                        status = "partial"
                    result = save_checkpoint(
                        status=status,
                        file_number=file_number,
                        offset=offset,
                    )
                    records_since_commit = 0
                    if result["status"] in {"stopped", "partial"}:
                        return result

            response.close()
            response = None
            record_offset = 0
            save_checkpoint(
                status="running",
                file_number=file_number + 1,
                offset=0,
            )

        return save_checkpoint(
            status="success",
            file_number=last_file + 1,
            offset=0,
            completed=True,
        )
    except Exception as exc:
        db.session.rollback()
        state = get_or_create_rpo_export_sync_state()
        state.status = "failed"
        state.last_error = f"{type(exc).__name__}: {exc}"
        db.session.commit()
        raise
    finally:
        if response is not None:
            response.close()
        session.close()


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

    detail_url = normalize_rpo_api_url(resource_url)

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
    company.sk_nace_code = normalized["sk_nace_code"]
    company.sk_nace_name = normalized["sk_nace_name"]
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


def find_rpo_source(rpo_id: int) -> CompanySource | None:
    """Find an RPO source through the selective external_id index."""

    matches = CompanySource.query.filter_by(external_id=str(rpo_id)).all()
    rpo_matches = [
        source for source in matches if source.source_type == "rpo2"
    ]
    if len(rpo_matches) > 1:
        raise RpoSyncError(f"Duplicitný RPO zdroj pre ID {rpo_id}.")
    return rpo_matches[0] if rpo_matches else None


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
    source_register = extract_source_register_name(record)

    if should_skip_rpo_record(normalized, source_register):
        logger.info(
            "RPO záznam %s sa ignoruje (právna forma=%r, register=%r).",
            rpo_id,
            normalized["legal_form"],
            source_register,
        )
        return None
    ico = normalized["ico"]

    # 1. Poznáme už presne tento RPO záznam?
    source = find_rpo_source(rpo_id)

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
            source_id=str(rpo_id),
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


def backfill_rpo_company_fields():
    """Doplní nové RPO polia pre firmy uložené pred rozšírením schémy."""
    updated_companies = 0
    seen_company_ids = set()

    for source in CompanySource.query.filter_by(source_type="rpo2").all():
        if source.company_id in seen_company_ids:
            continue

        normalized = normalized_rpo_fields(source.raw_data)
        company = source.company
        company.sk_nace_code = normalized["sk_nace_code"]
        company.sk_nace_name = normalized["sk_nace_name"]
        seen_company_ids.add(company.id)
        updated_companies += 1

    db.session.commit()

    return {
        "updated_companies": updated_companies,
    }


def create_initial_sync_url(
    state: SyncState,
    only_ids: bool,
    full_sync: bool = False,
) -> str:
    """
    Pri prvom importe bez last_successful_sync_at zavolá celý sync.

    Pri ďalších behoch použije čas poslednej kompletne úspešnej
    synchronizácie.
    """

    params: list[str] = []

    if state.last_successful_sync_at and not full_sync:
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
    full_sync: bool = False,
) -> dict[str, Any]:
    """
    Synchronizuje RPO2 do lokálnej databázy.

    max_records:
        Limit načítaných RPO záznamov. Synchronizácia sa zastaví
        až po celej stránke, aby sa dala bezpečne obnoviť.

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

    full_sync:
        Ignoruje posledný úspešný čas aj rozpracovanú next_url a načíta
        celú históriu. Používa sa pri rozšírení podporovaných typov subjektov.
    """

    if max_records is not None and max_records <= 0:
        raise ValueError("max_records musí byť kladné číslo.")

    if commit_every <= 0:
        raise ValueError("commit_every musí byť kladné číslo.")

    state = get_or_create_sync_state()
    session = build_http_session()

    current_run_started_at = utcnow()

    if resume and not full_sync and state.next_url:
        url = state.next_url

        if state.sync_started_at is None:
            state.sync_started_at = current_run_started_at
    else:
        state.sync_started_at = current_run_started_at
        state.processed_records = 0
        state.fetched_records = 0
        state.skipped_records = 0
        state.next_url = None

        url = create_initial_sync_url(
            state=state,
            only_ids=only_ids,
            full_sync=full_sync,
        )

    state.status = "running"
    state.last_error = None
    db.session.commit()

    run_fetched = 0
    run_imported = 0
    run_skipped = 0
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
                run_fetched += 1
                state.fetched_records += 1
                full_record = sync_record

                if only_ids:
                    full_record = fetch_record_detail(
                        session,
                        sync_record,
                    )

                    # Detail endpoint je ďalší request.
                    time.sleep(delay_seconds)

                company = upsert_rpo_record(full_record)

                if company is None:
                    run_skipped += 1
                    state.skipped_records += 1
                    continue

                run_imported += 1
                state.processed_records += 1

                if run_fetched % commit_every == 0:
                    db.session.commit()

            # Celá stránka bola bezpečne uložená.
            state.next_url = next_url
            db.session.commit()

            if max_records is not None and run_fetched >= max_records:
                state.status = "partial"
                state.last_error = (
                    "Synchronizácia zastavená cez max_records po celej "
                    "stránke. Ďalší beh bezpečne pokračuje z next_url."
                )
                db.session.commit()

                return {
                    "status": "partial",
                    "fetched": run_fetched,
                    "imported": run_imported,
                    "skipped": run_skipped,
                    "page": page_number,
                    "next_url": next_url,
                }

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
            "fetched": run_fetched,
            "imported": run_imported,
            "skipped": run_skipped,
            "pages": page_number,
            "completed_at": datetime_to_iso(completed_at),
        }

    except Exception as exc:
        db.session.rollback()

        # State získame znovu, pretože rollback mohol expirovať objekt.
        state = get_or_create_sync_state()
        state.status = "failed"
        state.last_error = (
            "RPO synchronizácia zlyhala. Podrobnosti sú v serverovom logu."
        )

        db.session.commit()

        logger.exception("RPO synchronizácia zlyhala.")

        raise RpoSyncError(state.last_error) from exc

    finally:
        session.close()


