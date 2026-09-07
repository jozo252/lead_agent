from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from extensions import db
from models import Company


logger = logging.getLogger(__name__)

RUZ_BASE_URL = "https://www.registeruz.sk/cruz-public"
MAX_STATEMENT_CANDIDATES = 8


class RuzApiError(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_ruz_session() -> requests.Session:
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session = requests.Session()
    session.mount("https://", adapter)
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": "LeadAgent-RUZ-Financials/1.0",
    })
    return session


class RuzClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        timeout_seconds: float = 20,
    ) -> None:
        self.session = session or build_ruz_session()
        self.timeout_seconds = timeout_seconds
        self._template_cache: dict[int, dict[str, Any]] = {}

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        try:
            response = self.session.get(
                f"{RUZ_BASE_URL}{path}",
                params=params,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuzApiError(f"RÚZ API požiadavka zlyhala: {exc}") from exc

        if not isinstance(payload, dict):
            raise RuzApiError("RÚZ API nevrátilo JSON objekt.")

        return payload

    def find_accounting_entity_ids(self, ico: str) -> list[int]:
        normalized_ico = re.sub(r"\D", "", ico)
        if normalized_ico:
            normalized_ico = normalized_ico.zfill(8)
        payload = self.get(
            "/api/uctovne-jednotky",
            **{
                "zmenene-od": "2000-01-01",
                "ico": normalized_ico,
                "max-zaznamov": 100,
            },
        )
        return [value for value in payload.get("id", []) if isinstance(value, int)]

    def accounting_entity(self, entity_id: int) -> dict[str, Any]:
        return self.get("/api/uctovna-jednotka", id=entity_id)

    def statement(self, statement_id: int) -> dict[str, Any]:
        return self.get("/api/uctovna-zavierka", id=statement_id)

    def report(self, report_id: int) -> dict[str, Any]:
        return self.get("/api/uctovny-vykaz", id=report_id)

    def template(self, template_id: int) -> dict[str, Any]:
        if template_id not in self._template_cache:
            self._template_cache[template_id] = self.get(
                "/api/sablona",
                id=template_id,
            )
        return self._template_cache[template_id]

    def close(self) -> None:
        self.session.close()


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text).strip().lower()


def parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    normalized = str(value).strip().replace(" ", "").replace(",", ".")
    if not normalized:
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def localized_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("sk") or value.get("en") or "")
    return str(value or "")


def table_current_values(
    report_table: dict[str, Any],
    template_table: dict[str, Any],
) -> list[tuple[str, Decimal | None]]:
    rows = template_table.get("riadky")
    data = report_table.get("data")
    if not isinstance(rows, list) or not rows or not isinstance(data, list):
        return []
    if len(data) % len(rows) != 0:
        return []

    values_per_row = len(data) // len(rows)
    if values_per_row <= 0:
        return []

    result = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        text = localized_name(row.get("text"))
        result.append((text, parse_decimal(data[index * values_per_row])))
    return result


def matching_template_table(
    report_table: dict[str, Any],
    template_tables: list[dict[str, Any]],
    index: int,
) -> dict[str, Any] | None:
    report_name = normalize_text(localized_name(report_table.get("nazov")))
    for template_table in template_tables:
        template_name = normalize_text(localized_name(template_table.get("nazov")))
        if report_name and template_name == report_name:
            return template_table
    if index < len(template_tables):
        return template_tables[index]
    return None


def extract_financial_metrics(
    report: dict[str, Any],
    template: dict[str, Any],
) -> dict[str, Decimal | None]:
    metrics: dict[str, Decimal | None] = {
        "revenue": None,
        "total_income": None,
        "profit": None,
    }
    operating_income = None
    financial_income = None
    sales_parts: list[Decimal] = []

    report_tables = report.get("obsah", {}).get("tabulky", [])
    template_tables = template.get("tabulky", [])
    if not isinstance(report_tables, list) or not isinstance(template_tables, list):
        return metrics

    for index, report_table in enumerate(report_tables):
        if not isinstance(report_table, dict):
            continue
        template_table = matching_template_table(report_table, template_tables, index)
        if not template_table:
            continue

        for label, value in table_current_values(report_table, template_table):
            normalized = normalize_text(label)
            if normalized.startswith("cisty obrat"):
                metrics["revenue"] = value
            elif normalized.startswith("vysledok hospodarenia za uctovne obdobie po zdaneni"):
                metrics["profit"] = value
            elif normalized.startswith("vynosy z hospodarskej cinnosti spolu"):
                operating_income = value
            elif normalized.startswith("vynosy z financnej cinnosti spolu"):
                financial_income = value
            elif normalized in {"vynosy spolu", "celkove vynosy"}:
                metrics["total_income"] = value
            elif normalized.startswith((
                "trzby z predaja tovaru",
                "trzby z predaja vlastnych vyrobkov",
                "trzby z predaja sluzieb",
            )) and value is not None:
                sales_parts.append(value)

    if metrics["revenue"] is None and sales_parts:
        metrics["revenue"] = sum(sales_parts, Decimal("0"))

    if metrics["total_income"] is None:
        available_income = [
            value
            for value in (operating_income, financial_income)
            if value is not None
        ]
        if available_income:
            metrics["total_income"] = sum(available_income, Decimal("0"))

    currency = normalize_text(report.get("mena"))
    if "tis" in currency:
        for name, value in metrics.items():
            if value is not None:
                metrics[name] = value * 1000

    return metrics


def parse_iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def period_sort_key(statement: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(statement.get("obdobieDo") or ""),
        str(statement.get("datumPodania") or ""),
        int(statement.get("id") or 0),
    )


def latest_statement(
    client: RuzClient,
    statement_ids: list[int],
) -> dict[str, Any] | None:
    candidates = []
    unique_ids = sorted(set(statement_ids), reverse=True)[:MAX_STATEMENT_CANDIDATES]
    for statement_id in unique_ids:
        statement = client.statement(statement_id)
        if statement.get("stav") == "ZMAZANÉ":
            continue
        if statement.get("konsolidovana") is True:
            continue
        if statement.get("typ") not in {"Riadna", "Kombinovaná"}:
            continue
        if not statement.get("idUctovnychVykazov"):
            continue
        candidates.append(statement)

    return max(candidates, key=period_sort_key) if candidates else None


def fetch_company_financials(
    client: RuzClient,
    ico: str,
) -> dict[str, Any]:
    entity_ids = client.find_accounting_entity_ids(ico)
    if not entity_ids:
        return {"status": "not_found"}

    entities = [client.accounting_entity(entity_id) for entity_id in entity_ids]
    entities = [entity for entity in entities if entity.get("stav") != "ZMAZANÉ"]
    if not entities:
        return {"status": "not_found"}

    entity = max(
        entities,
        key=lambda item: (
            len(item.get("idUctovnychZavierok", [])),
            str(item.get("datumPoslednejUpravy") or ""),
            int(item.get("id") or 0),
        ),
    )
    statement = latest_statement(client, entity.get("idUctovnychZavierok", []))
    if statement is None:
        return {
            "status": "no_public_statement",
            "accounting_entity_id": entity.get("id"),
        }

    merged: dict[str, Decimal | None] = {
        "revenue": None,
        "total_income": None,
        "profit": None,
    }
    used_report_id = None
    for report_id in statement.get("idUctovnychVykazov", []):
        report = client.report(report_id)
        if report.get("pristupnostDat") not in {None, "Verejné"}:
            continue
        template_id = report.get("idSablony")
        if not isinstance(template_id, int) or not report.get("obsah"):
            continue
        metrics = extract_financial_metrics(report, client.template(template_id))
        if any(value is not None for value in metrics.values()):
            used_report_id = report_id
        for name, value in metrics.items():
            if value is not None:
                merged[name] = value

    period_end = str(statement.get("obdobieDo") or "")
    try:
        financial_year = int(period_end[:4])
    except (TypeError, ValueError):
        financial_year = None

    status = "success" if any(value is not None for value in merged.values()) else "unsupported_statement"
    statement_id = statement.get("id")
    return {
        "status": status,
        "accounting_entity_id": entity.get("id"),
        "statement_id": statement_id,
        "report_id": used_report_id,
        "financial_year": financial_year,
        "submitted_on": parse_iso_date(statement.get("datumPodania")),
        "revenue": merged["revenue"],
        "total_income": merged["total_income"],
        "profit": merged["profit"],
        "source_url": f"{RUZ_BASE_URL}/api/uctovna-zavierka?id={statement_id}",
    }


def apply_company_financials(company: Company, result: dict[str, Any]) -> None:
    company.financials_status = result["status"]
    company.financials_checked_at = utcnow()
    company.ruz_accounting_entity_id = result.get("accounting_entity_id")

    if result["status"] != "success":
        return

    company.financial_year = result.get("financial_year")
    company.annual_revenue = result.get("revenue")
    company.annual_total_income = result.get("total_income")
    company.annual_profit = result.get("profit")
    company.financial_statement_submitted_on = result.get("submitted_on")
    company.financial_statement_id = result.get("statement_id")
    company.financial_report_id = result.get("report_id")
    company.financial_source_url = result.get("source_url")


def enrich_company_financials(
    max_companies: int | None = 10,
    include_existing: bool = False,
    companies: list[Company] | None = None,
    delay_seconds: float = 0.1,
    client: RuzClient | None = None,
) -> dict[str, Any]:
    if companies is None:
        query = Company.query.filter(Company.ico.isnot(None)).order_by(
            Company.official_name,
            Company.ico,
        )
        if not include_existing:
            query = query.filter(Company.financials_checked_at.is_(None))
        if max_companies is not None:
            query = query.limit(max_companies)
        companies = query.all()

    summary = {
        "processed_companies": 0,
        "companies_with_financials": 0,
        "companies_without_financials": 0,
        "errors": [],
    }
    owns_client = client is None
    client = client or RuzClient()

    try:
        for company in companies:
            try:
                result = fetch_company_financials(client, company.ico or "")
                apply_company_financials(company, result)
                db.session.commit()
            except Exception:
                db.session.rollback()
                logger.exception("RÚZ financie zlyhali pre IČO %s", company.ico)
                summary["errors"].append({
                    "ico": company.ico,
                    "error": "Načítanie finančných údajov zlyhalo. Podrobnosti sú v serverovom logu.",
                })
                continue

            summary["processed_companies"] += 1
            if result["status"] == "success":
                summary["companies_with_financials"] += 1
            else:
                summary["companies_without_financials"] += 1

            if delay_seconds:
                time.sleep(delay_seconds)
    finally:
        if owns_client:
            client.close()

    return summary
