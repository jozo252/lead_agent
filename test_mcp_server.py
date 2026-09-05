import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError

import mcp_server
from app import create_app
from extensions import db
from models import Campaign, Lead, Opportunity, OutboundEmail, QuoteRequest


class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "SQLALCHEMY_DATABASE_URI": "sqlite://",
                "WTF_CSRF_ENABLED": False,
            }
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.original_flask_app = mcp_server.flask_app
        mcp_server.flask_app = self.app

        self.campaign = Campaign(
            name="Stavebné zákazky",
            offer_type="service",
            offer_description="Stavebné práce",
            subject_template="Spolupráca pre {company_name}",
            body_template=(
                "Ponúkame kapacitu pre {company_name} v lokalite {municipality}."
            ),
            business_line="construction",
            status="active",
            daily_limit=5,
        )
        db.session.add(self.campaign)
        db.session.flush()
        self.opportunity = Opportunity(
            campaign=self.campaign,
            business_line="construction",
            source_name="brave_web",
            source_url="https://example.sk/dopyt/7",
            search_query="stavebné práce dopyt",
            title="Aktuálny dopyt na stavebné práce",
            description="Hľadáme subdodávateľa.",
            status="new",
            fit_score=80,
            fit_reason="Signál zákazky: dopyt",
            evidence=["Aktuálny dopyt", "Verejný zdroj"],
            discovered_at=datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc),
            last_seen_at=datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc),
        )
        db.session.add(self.opportunity)
        db.session.commit()

    def tearDown(self):
        mcp_server.flask_app = self.original_flask_app
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def conversion_arguments(self, *, confirmed=True):
        return {
            "opportunity_id": self.opportunity.id,
            "verification_confirmed": confirmed,
            "company_name": "Overená stavebná firma",
            "verification_note": (
                "Dopyt aj firemný kontakt overené na zdroji 4. 9. 2026."
            ),
            "contact_source_url": "https://example.sk/kontakt?utm_source=test",
            "email": "Zakazky@Example.sk",
            "phone": None,
            "website": "https://example.sk",
            "city": "Poprad",
        }

    def test_server_exposes_only_the_six_scoped_tools(self):
        tools = asyncio.run(mcp_server.server.list_tools())
        tools_by_name = {tool.name: tool for tool in tools}

        self.assertEqual(
            set(tools_by_name),
            {
                "list_new_opportunities",
                "convert_verified_opportunity",
                "list_new_leads",
                "list_quote_requests",
                "campaign_metrics",
                "pause_campaign",
            },
        )
        self.assertTrue(
            tools_by_name["list_new_opportunities"].annotations.read_only_hint
        )
        self.assertFalse(
            tools_by_name["convert_verified_opportunity"].annotations.read_only_hint
        )
        self.assertIn(
            "email_sent",
            tools_by_name["convert_verified_opportunity"].output_schema["properties"],
        )
        self.assertFalse(any("send" in name for name in tools_by_name))

        call_result = asyncio.run(
            mcp_server.server.call_tool(
                "list_new_opportunities",
                {"campaign_id": self.campaign.id, "limit": 10},
            )
        )
        self.assertFalse(call_result.is_error)
        self.assertEqual(call_result.structured_content["count"], 1)

    def test_stdio_client_completes_handshake_and_lists_tools(self):
        async def list_tool_names():
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["mcp_server.py"],
                cwd=Path(__file__).resolve().parent,
                env={**os.environ, "DATABASE_URL": "sqlite://"},
            )
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return {tool.name for tool in result.tools}

        self.assertEqual(
            asyncio.run(list_tool_names()),
            {
                "list_new_opportunities",
                "convert_verified_opportunity",
                "list_new_leads",
                "list_quote_requests",
                "campaign_metrics",
                "pause_campaign",
            },
        )

    def test_conversion_requires_confirmation_and_never_sends_email(self):
        with self.assertRaises(ToolError):
            mcp_server.convert_verified_opportunity(
                **self.conversion_arguments(confirmed=False)
            )

        self.assertEqual(Lead.query.count(), 0)
        self.assertEqual(OutboundEmail.query.count(), 0)
        self.assertEqual(self.opportunity.status, "new")

        result = mcp_server.convert_verified_opportunity(
            **self.conversion_arguments()
        )

        self.assertTrue(result.created)
        self.assertFalse(result.email_sent)
        self.assertEqual(result.lead_status, "Osloviť")
        self.assertIn("Overená stavebná firma", result.suggested_message)
        self.assertEqual(Lead.query.count(), 1)
        self.assertEqual(OutboundEmail.query.count(), 0)
        db.session.expire_all()
        opportunity = db.session.get(Opportunity, self.opportunity.id)
        self.assertEqual(opportunity.status, "converted")

        repeated = mcp_server.convert_verified_opportunity(
            **self.conversion_arguments()
        )
        self.assertFalse(repeated.created)
        self.assertFalse(repeated.email_sent)
        self.assertEqual(Lead.query.count(), 1)
        self.assertEqual(OutboundEmail.query.count(), 0)

    def test_read_tools_return_opportunities_leads_quotes_and_metrics(self):
        opportunities = mcp_server.list_new_opportunities(
            campaign_id=self.campaign.id
        )
        self.assertEqual(opportunities.count, 1)
        self.assertEqual(opportunities.opportunities[0].fit_score, 80)

        conversion = mcp_server.convert_verified_opportunity(
            **self.conversion_arguments()
        )
        leads = mcp_server.list_new_leads(campaign_id=self.campaign.id)
        self.assertEqual(leads.count, 1)
        self.assertEqual(leads.leads[0].id, conversion.lead_id)

        quote = QuoteRequest(
            lead_id=conversion.lead_id,
            recipient_email="zakazky@example.sk",
            request_text="Pošlite cenovú ponuku.",
            status="awaiting_price",
        )
        db.session.add(quote)
        db.session.commit()

        quote_requests = mcp_server.list_quote_requests()
        self.assertEqual(quote_requests.count, 1)
        self.assertIsNone(quote_requests.quote_requests[0].amount)

        metrics = mcp_server.campaign_metrics(campaign_id=self.campaign.id)
        self.assertEqual(metrics.count, 1)
        self.assertEqual(metrics.campaigns[0].converted_leads, 1)
        self.assertEqual(metrics.campaigns[0].opportunity_counts, {"converted": 1})
        self.assertEqual(
            metrics.campaigns[0].quote_request_counts,
            {"awaiting_price": 1},
        )

    def test_pause_campaign_requires_confirmation_and_is_idempotent(self):
        with self.assertRaises(ToolError):
            mcp_server.pause_campaign(
                campaign_id=self.campaign.id,
                confirmation=False,
            )
        self.assertEqual(self.campaign.status, "active")

        result = mcp_server.pause_campaign(
            campaign_id=self.campaign.id,
            confirmation=True,
        )
        self.assertTrue(result.changed)
        self.assertEqual(result.status, "paused")

        repeated = mcp_server.pause_campaign(
            campaign_id=self.campaign.id,
            confirmation=False,
        )
        self.assertFalse(repeated.changed)
        self.assertEqual(repeated.status, "paused")


if __name__ == "__main__":
    unittest.main()
