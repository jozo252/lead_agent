import unittest
from unittest.mock import patch

from app import create_app
from extensions import db
from models import Company


class CompanyContactEnrichmentRouteTests(unittest.TestCase):
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

    @patch("routes.enrich_company_contacts")
    def test_detail_button_posts_single_company_enrichment(self, enrich_mock):
        company = Company(ico="12345678", official_name="Test, s.r.o.")
        db.session.add(company)
        db.session.commit()
        enrich_mock.return_value = {
            "processed_companies": 1,
            "companies_with_contacts": 1,
            "companies_without_contacts": 0,
            "saved_contacts": 2,
            "errors": [],
        }

        detail_response = self.app.test_client().get(
            f"/companies/{company.id}",
        )
        post_response = self.app.test_client().post(
            f"/companies/{company.id}/enrich-contacts",
        )

        self.assertEqual(detail_response.status_code, 200)
        self.assertIn(
            f'/companies/{company.id}/enrich-contacts'.encode(),
            detail_response.data,
        )
        self.assertEqual(post_response.status_code, 302)
        self.assertIn(
            f"/companies/{company.id}",
            post_response.headers["Location"],
        )
        enrich_mock.assert_called_once()
        self.assertEqual(enrich_mock.call_args.kwargs["companies"], [company])
        self.assertEqual(enrich_mock.call_args.kwargs["delay_seconds"], 0)

    @patch("routes.enrich_company_contacts")
    def test_enrichment_error_is_shown_after_redirect(self, enrich_mock):
        company = Company(ico="12345678", official_name="Test, s.r.o.")
        db.session.add(company)
        db.session.commit()
        enrich_mock.return_value = {
            "processed_companies": 0,
            "companies_with_contacts": 0,
            "companies_without_contacts": 0,
            "saved_contacts": 0,
            "errors": [{"ico": company.ico, "error": "Brave API offline"}],
        }

        response = self.app.test_client().post(
            f"/companies/{company.id}/enrich-contacts",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Brave API offline", response.data)
        self.assertIn(b"serverovom logu", response.data)


if __name__ == "__main__":
    unittest.main()
