from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from flask import Flask
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from sqlalchemy import func, inspect
from sqlalchemy.engine import make_url

from extensions import db
from models import (
    Campaign,
    CampaignRecipient,
    Lead,
    LeadActivity,
    Opportunity,
    QuoteRequest,
)
from services.opportunity_conversion import (
    convert_verified_opportunity as convert_opportunity_service,
)


Limit = Annotated[int, Field(ge=1, le=100)]
LeadStatus = Literal[
    "Nový",
    "Skontrolovať",
    "Osloviť",
    "Oslovený",
    "Odpovedal",
    "Telefonát",
    "Obhliadka",
    "Cenová ponuka",
    "Vyhraté",
    "Prehraté",
    "Nezaujímavé",
]
QuoteStatus = Literal[
    "awaiting_price",
    "ready",
    "sending",
    "sent",
    "unknown",
    "cancelled",
    "suppressed",
]
NEW_LEAD_STATUSES = ("Nový", "Skontrolovať", "Osloviť")
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "instance" / "leads.db"
MCP_DATABASE_URL_ENV = "LEAD_AGENT_MCP_DATABASE_URL"
MCP_MODELS = (
    Campaign,
    CampaignRecipient,
    Lead,
    LeadActivity,
    Opportunity,
    QuoteRequest,
)
REQUIRED_SCHEMA = {
    model.__table__.name: {column.name for column in model.__table__.columns}
    for model in MCP_MODELS
}


class OpportunityItem(BaseModel):
    id: int
    campaign_id: int
    campaign_name: str
    business_line: str
    title: str
    description: str | None
    source_url: str
    published_text: str | None
    fit_score: int
    fit_reason: str | None
    evidence: list[str]
    discovered_at: str | None
    last_seen_at: str | None


class OpportunityListResult(BaseModel):
    count: int
    opportunities: list[OpportunityItem]


class LeadItem(BaseModel):
    id: int
    company_name: str
    status: str
    email: str | None
    phone: str | None
    city: str | None
    work_type: str | None
    lead_score: int | None
    source: str | None
    source_opportunity_id: int | None
    suggested_subject: str | None
    suggested_message: str | None
    created_at: str | None


class LeadListResult(BaseModel):
    count: int
    leads: list[LeadItem]


class QuoteRequestItem(BaseModel):
    id: int
    lead_id: int
    company_name: str
    recipient_email: str
    request_text: str | None
    status: str
    amount: str | None
    currency: str | None
    unit: str | None
    vat_text: str | None
    validity_days: int | None
    created_at: str | None
    updated_at: str | None


class QuoteRequestListResult(BaseModel):
    count: int
    quote_requests: list[QuoteRequestItem]


class CampaignMetricItem(BaseModel):
    id: int
    name: str
    business_line: str
    status: str
    recipient_counts: dict[str, int]
    opportunity_counts: dict[str, int]
    converted_leads: int
    quote_request_counts: dict[str, int]


class CampaignMetricsResult(BaseModel):
    count: int
    campaigns: list[CampaignMetricItem]


class OpportunityConversionResult(BaseModel):
    created: bool
    opportunity_id: int
    lead_id: int
    lead_status: str
    company_name: str
    suggested_subject: str | None
    suggested_message: str | None
    email_sent: Literal[False]


class CampaignPauseResult(BaseModel):
    campaign_id: int
    campaign_name: str
    previous_status: str
    status: str
    changed: bool


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
INTERNAL_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


server = MCPServer(
    name="lead-agent",
    title="Lead Agent CRM",
    version="0.1.0",
    instructions=(
        "Tento server nikdy neposiela e-maily, cenové ponuky ani nepublikuje "
        "obsah. Pred každým zápisom musí používateľ výslovne potvrdiť konkrétnu "
        "akciu. Príležitosť konvertuj iba po manuálnom overení zdroja a kontaktu. "
        "Nevymýšľaj cenu, kapacitu, kvalifikáciu ani identitu odosielateľa."
    ),
)


flask_app: Flask | None = None


def _validate_sqlite_database_url(database_url: str) -> None:
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "sqlite":
        return
    database = parsed.database
    if database in (None, "", ":memory:"):
        return
    database_path = Path(database)
    if not database_path.is_absolute():
        raise RuntimeError(
            f"{MCP_DATABASE_URL_ENV} musí pre SQLite obsahovať absolútnu cestu."
        )
    if not database_path.is_file():
        raise RuntimeError(
            f"MCP databáza neexistuje: {database_path}. "
            f"Skontroluj {MCP_DATABASE_URL_ENV}."
        )


def _resolve_database_uri() -> str:
    configured = str(os.environ.get(MCP_DATABASE_URL_ENV) or "").strip()
    if configured:
        _validate_sqlite_database_url(configured)
        return configured
    if not DEFAULT_DATABASE_PATH.is_file():
        raise RuntimeError(
            f"MCP databáza neexistuje: {DEFAULT_DATABASE_PATH}. "
            f"Nastav {MCP_DATABASE_URL_ENV} na explicitnú databázu."
        )
    return f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}"


def _create_database_app(database_uri: str | None = None) -> Flask:
    app = Flask(
        "lead_agent_mcp",
        instance_path=str(PROJECT_ROOT / "instance"),
    )
    app.config.update(
        SQLALCHEMY_DATABASE_URI=database_uri or _resolve_database_uri(),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    return app


def _get_flask_app() -> Flask:
    global flask_app
    if flask_app is None:
        flask_app = _create_database_app()
    return flask_app


def validate_database_schema(app: Flask | None = None) -> None:
    checked_app = app or _get_flask_app()
    with checked_app.app_context():
        database_inspector = inspect(db.engine)
        available_tables = set(database_inspector.get_table_names())
        problems = []
        for table_name, required_columns in REQUIRED_SCHEMA.items():
            if table_name not in available_tables:
                problems.append(f"chýba tabuľka {table_name}")
                continue
            available_columns = {
                column["name"]
                for column in database_inspector.get_columns(table_name)
            }
            missing_columns = sorted(required_columns - available_columns)
            if missing_columns:
                problems.append(
                    f"{table_name} nemá stĺpce {', '.join(missing_columns)}"
                )
        if problems:
            raise RuntimeError(
                "MCP databáza nemá požadovanú migračnú schému: "
                + "; ".join(problems)
                + ". Najprv bezpečne spusti Flask-Migrate upgrade."
            )


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _decimal(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _counts(query) -> dict[str, int]:
    return {str(status): int(count) for status, count in query.all()}


@server.tool(
    name="list_new_opportunities",
    title="Zoznam nových príležitostí",
    description=(
        "Zobrazí neprevedené príležitosti nájdené lovcom zákaziek. "
        "Použi pred manuálnou kontrolou zdroja."
    ),
    annotations=READ_ONLY,
    structured_output=True,
)
def list_new_opportunities(
    campaign_id: int | None = None,
    limit: Limit = 20,
) -> OpportunityListResult:
    with _get_flask_app().app_context():
        query = Opportunity.query.filter_by(status="new")
        if campaign_id is not None:
            query = query.filter(Opportunity.campaign_id == campaign_id)
        opportunities = query.order_by(
            Opportunity.fit_score.desc(),
            Opportunity.discovered_at.desc(),
        ).limit(limit).all()
        items = [
            OpportunityItem(
                id=item.id,
                campaign_id=item.campaign_id,
                campaign_name=item.campaign.name,
                business_line=item.business_line,
                title=item.title,
                description=item.description,
                source_url=item.source_url,
                published_text=item.published_text,
                fit_score=item.fit_score,
                fit_reason=item.fit_reason,
                evidence=list(item.evidence or []),
                discovered_at=_iso(item.discovered_at),
                last_seen_at=_iso(item.last_seen_at),
            )
            for item in opportunities
        ]
        return OpportunityListResult(count=len(items), opportunities=items)


@server.tool(
    name="convert_verified_opportunity",
    title="Prevod overenej príležitosti na lead",
    description=(
        "Vytvorí CRM lead a koncept oslovenia bez odoslania. Použi iba keď "
        "používateľ výslovne potvrdil, že manuálne overil aktuálnosť aj kontakt."
    ),
    annotations=INTERNAL_WRITE,
    structured_output=True,
)
def convert_verified_opportunity(
    opportunity_id: int,
    verification_confirmed: bool,
    company_name: str,
    verification_note: str,
    contact_source_url: str,
    email: str | None = None,
    phone: str | None = None,
    website: str | None = None,
    city: str | None = None,
) -> OpportunityConversionResult:
    with _get_flask_app().app_context():
        opportunity = db.session.get(Opportunity, opportunity_id)
        if opportunity is None:
            raise ToolError("Príležitosť neexistuje.")
        try:
            lead, created = convert_opportunity_service(
                opportunity,
                verification_confirmed=verification_confirmed,
                company_name=company_name,
                verification_note=verification_note,
                contact_source_url=contact_source_url,
                email=email,
                phone=phone,
                website=website,
                city=city,
            )
            if created:
                db.session.commit()
            return OpportunityConversionResult(
                created=created,
                opportunity_id=opportunity.id,
                lead_id=lead.id,
                lead_status=lead.status,
                company_name=lead.company_name,
                suggested_subject=lead.suggested_subject,
                suggested_message=lead.suggested_message,
                email_sent=False,
            )
        except ValueError as exc:
            db.session.rollback()
            raise ToolError(str(exc)) from exc
        except Exception:
            db.session.rollback()
            raise


@server.tool(
    name="list_new_leads",
    title="Zoznam nových CRM leadov",
    description=(
        "Zobrazí nové alebo pripravené leady vrátane uloženého konceptu. "
        "Nič neodosiela."
    ),
    annotations=READ_ONLY,
    structured_output=True,
)
def list_new_leads(
    campaign_id: int | None = None,
    status: LeadStatus | None = None,
    limit: Limit = 20,
) -> LeadListResult:
    with _get_flask_app().app_context():
        query = Lead.query
        if campaign_id is not None:
            query = query.join(
                Opportunity,
                Opportunity.lead_id == Lead.id,
            ).filter(Opportunity.campaign_id == campaign_id)
        if status is None:
            query = query.filter(Lead.status.in_(NEW_LEAD_STATUSES))
        else:
            query = query.filter(Lead.status == status)
        leads = query.order_by(Lead.created_at.desc()).limit(limit).all()
        items = [
            LeadItem(
                id=lead.id,
                company_name=lead.company_name,
                status=lead.status,
                email=lead.email,
                phone=lead.phone,
                city=lead.city,
                work_type=lead.work_type,
                lead_score=lead.lead_score,
                source=lead.source,
                source_opportunity_id=(
                    lead.source_opportunity.id if lead.source_opportunity else None
                ),
                suggested_subject=lead.suggested_subject,
                suggested_message=lead.suggested_message,
                created_at=_iso(lead.created_at),
            )
            for lead in leads
        ]
        return LeadListResult(count=len(items), leads=items)


@server.tool(
    name="list_quote_requests",
    title="Zoznam cenových požiadaviek",
    description=(
        "Zobrazí cenové požiadavky a ich stav. Nástroj cenu nedopĺňa a ponuku "
        "neodosiela."
    ),
    annotations=READ_ONLY,
    structured_output=True,
)
def list_quote_requests(
    status: QuoteStatus | None = "awaiting_price",
    limit: Limit = 20,
) -> QuoteRequestListResult:
    with _get_flask_app().app_context():
        query = QuoteRequest.query
        if status is not None:
            query = query.filter(QuoteRequest.status == status)
        quote_requests = query.order_by(QuoteRequest.created_at.desc()).limit(limit).all()
        items = [
            QuoteRequestItem(
                id=item.id,
                lead_id=item.lead_id,
                company_name=item.lead.company_name,
                recipient_email=item.recipient_email,
                request_text=item.request_text,
                status=item.status,
                amount=_decimal(item.amount),
                currency=item.currency,
                unit=item.unit,
                vat_text=item.vat_text,
                validity_days=item.validity_days,
                created_at=_iso(item.created_at),
                updated_at=_iso(item.updated_at),
            )
            for item in quote_requests
        ]
        return QuoteRequestListResult(count=len(items), quote_requests=items)


@server.tool(
    name="campaign_metrics",
    title="Metriky kampaní",
    description=(
        "Vráti počty príjemcov, príležitostí, konvertovaných leadov a cenových "
        "požiadaviek pre jednu alebo všetky kampane."
    ),
    annotations=READ_ONLY,
    structured_output=True,
)
def campaign_metrics(campaign_id: int | None = None) -> CampaignMetricsResult:
    with _get_flask_app().app_context():
        query = Campaign.query
        if campaign_id is not None:
            query = query.filter(Campaign.id == campaign_id)
        campaigns = query.order_by(Campaign.created_at.desc()).limit(100).all()
        items = []
        for campaign in campaigns:
            recipient_counts = _counts(
                db.session.query(
                    CampaignRecipient.status,
                    func.count(CampaignRecipient.id),
                )
                .filter(CampaignRecipient.campaign_id == campaign.id)
                .group_by(CampaignRecipient.status)
            )
            opportunity_counts = _counts(
                db.session.query(
                    Opportunity.status,
                    func.count(Opportunity.id),
                )
                .filter(Opportunity.campaign_id == campaign.id)
                .group_by(Opportunity.status)
            )
            quote_request_counts = _counts(
                db.session.query(
                    QuoteRequest.status,
                    func.count(QuoteRequest.id),
                )
                .join(Opportunity, Opportunity.lead_id == QuoteRequest.lead_id)
                .filter(Opportunity.campaign_id == campaign.id)
                .group_by(QuoteRequest.status)
            )
            converted_leads = Opportunity.query.filter(
                Opportunity.campaign_id == campaign.id,
                Opportunity.lead_id.isnot(None),
            ).count()
            items.append(
                CampaignMetricItem(
                    id=campaign.id,
                    name=campaign.name,
                    business_line=campaign.business_line,
                    status=campaign.status,
                    recipient_counts=recipient_counts,
                    opportunity_counts=opportunity_counts,
                    converted_leads=converted_leads,
                    quote_request_counts=quote_request_counts,
                )
            )
        return CampaignMetricsResult(count=len(items), campaigns=items)


@server.tool(
    name="pause_campaign",
    title="Pozastavenie kampane",
    description=(
        "Pozastaví aktívnu kampaň bez odoslania správ. Použi iba po výslovnom "
        "pokyne používateľa pre konkrétne ID kampane."
    ),
    annotations=INTERNAL_WRITE,
    structured_output=True,
)
def pause_campaign(
    campaign_id: int,
    confirmation: bool,
) -> CampaignPauseResult:
    with _get_flask_app().app_context():
        campaign = db.session.get(Campaign, campaign_id)
        if campaign is None:
            raise ToolError("Kampaň neexistuje.")
        previous_status = campaign.status
        if campaign.status == "paused":
            return CampaignPauseResult(
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                previous_status=previous_status,
                status=campaign.status,
                changed=False,
            )
        if not confirmation:
            raise ToolError("Pozastavenie vyžaduje výslovné potvrdenie používateľa.")
        if campaign.status != "active":
            raise ToolError("Pozastaviť možno iba aktívnu kampaň.")
        campaign.status = "paused"
        db.session.commit()
        return CampaignPauseResult(
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            previous_status=previous_status,
            status=campaign.status,
            changed=True,
        )


if __name__ == "__main__":
    validate_database_schema()
    server.run(transport="stdio")
