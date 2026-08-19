import unittest
from decimal import Decimal
from unittest.mock import patch

from app import create_app
from extensions import db
from models import Company
from services.ruz_financials import (
    RuzApiError,
    enrich_company_financials,
    extract_financial_metrics,
    fetch_company_financials,
)


INCOME_ROWS = [
    "Čistý obrat (časť účt. tr. 6 podľa zákona)",
    "Výnosy z hospodárskej činnosti spolu",
    "Výnosy z finančnej činnosti spolu",
    "Výsledok hospodárenia za účtovné obdobie po zdanení",
]


def financial_template():
    return {
        "id": 699,
        "tabulky": [{
            "nazov": {"sk": "Výkaz ziskov a strát"},
            "riadky": [
                {"cisloRiadku": index, "text": {"sk": text}}
                for index, text in enumerate(INCOME_ROWS, start=1)
            ],
        }],
    }


def financial_report():
    return {
        "id": 900,
        "idSablony": 699,
        "pristupnostDat": "Verejné",
        "obsah": {
            "tabulky": [{
                "nazov": {"sk": "Výkaz ziskov a strát"},
                "data": [
                    "100000", "90000",
                    "105000", "95000",
                    "500", "400",
                    "12000", "10000",
                ],
            }],
        },
    }


class FakeRuzClient:
    def __init__(self, fail=False):
        self.fail = fail

    def find_accounting_entity_ids(self, ico):
        if self.fail:
            raise RuzApiError("offline")
        return [100]

    def accounting_entity(self, entity_id):
        return {
            "id": entity_id,
            "ico": "12345678",
            "idUctovnychZavierok": [700],
        }

    def statement(self, statement_id):
        return {
            "id": statement_id,
            "typ": "Riadna",
            "obdobieDo": "2025-12",
            "datumPodania": "2026-03-25",
            "idUctovnychVykazov": [900],
        }

    def report(self, report_id):
        return financial_report()

    def template(self, template_id):
        return financial_template()

    def close(self):
        pass


class RuzFinancialParserTests(unittest.TestCase):
    def test_extracts_current_period_values(self):
        metrics = extract_financial_metrics(
            financial_report(),
            financial_template(),
        )

        self.assertEqual(metrics["revenue"], Decimal("100000"))
        self.assertEqual(metrics["total_income"], Decimal("105500"))
        self.assertEqual(metrics["profit"], Decimal("12000"))

    def test_fetches_latest_statement_metadata(self):
        result = fetch_company_financials(FakeRuzClient(), "12345678")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["financial_year"], 2025)
        self.assertEqual(result["statement_id"], 700)
        self.assertEqual(result["report_id"], 900)


class RuzFinancialEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "WTF_CSRF_ENABLED": False,
        })
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_saves_financials_on_success(self):
        company = Company(ico="12345678", official_name="Test, s.r.o.")
        db.session.add(company)
        db.session.commit()

        summary = enrich_company_financials(
            companies=[company],
            delay_seconds=0,
            client=FakeRuzClient(),
        )

        self.assertEqual(summary["companies_with_financials"], 1)
        self.assertEqual(company.financial_year, 2025)
        self.assertEqual(company.annual_revenue, Decimal("100000.00"))
        self.assertEqual(company.annual_profit, Decimal("12000.00"))
        self.assertIsNotNone(company.financials_checked_at)

    def test_network_error_does_not_mark_company_checked(self):
        company = Company(ico="12345678", official_name="Test, s.r.o.")
        db.session.add(company)
        db.session.commit()

        summary = enrich_company_financials(
            companies=[company],
            delay_seconds=0,
            client=FakeRuzClient(fail=True),
        )

        self.assertEqual(len(summary["errors"]), 1)
        self.assertIsNone(company.financials_checked_at)

    def test_company_pages_render_saved_revenue(self):
        company = Company(
            ico="12345678",
            official_name="Test, s.r.o.",
            financial_year=2025,
            annual_revenue=Decimal("3790817"),
            financials_status="success",
        )
        db.session.add(company)
        db.session.commit()

        list_response = self.app.test_client().get("/companies")
        detail_response = self.app.test_client().get(f"/companies/{company.id}")

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(detail_response.status_code, 200)
        expected = "3 790 817 €".encode("utf-8")
        self.assertIn(expected, list_response.data)
        self.assertIn(expected, detail_response.data)

    @patch("routes.enrich_company_financials")
    def test_detail_button_posts_single_company_enrichment(self, enrich_mock):
        company = Company(ico="12345678", official_name="Test, s.r.o.")
        db.session.add(company)
        db.session.commit()
        enrich_mock.return_value = {
            "processed_companies": 1,
            "companies_with_financials": 1,
            "companies_without_financials": 0,
            "errors": [],
        }

        response = self.app.test_client().post(
            f"/companies/{company.id}/enrich-financials",
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/companies/{company.id}", response.headers["Location"])
        enrich_mock.assert_called_once()
        self.assertEqual(enrich_mock.call_args.kwargs["companies"], [company])
        self.assertEqual(enrich_mock.call_args.kwargs["delay_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
