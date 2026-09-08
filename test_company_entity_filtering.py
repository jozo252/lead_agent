import unittest

from app import create_app
from extensions import db
from models import Company
from services.company_filtering import (
    company_filters_from_source,
    filtered_companies_query,
)


class CompanyEntityFilteringTests(unittest.TestCase):
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
        db.session.add_all(
            [
                Company(
                    ico="10000001",
                    official_name="Firma, s. r. o.",
                    legal_form="Spoločnosť s ručením obmedzeným",
                ),
                Company(
                    ico="10000002",
                    official_name="Ján Test",
                    legal_form=(
                        "Podnikateľ-fyzická osoba-nezapísaný "
                        "v obchodnom registri"
                    ),
                ),
            ]
        )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_default_and_explicit_entity_lists_are_separate(self):
        default_result = filtered_companies_query(
            company_filters_from_source({})
        ).all()
        trader_result = filtered_companies_query(
            company_filters_from_source({"entity": "sole_trader"})
        ).all()
        all_result = filtered_companies_query(
            company_filters_from_source({"entity": "all"})
        ).all()

        self.assertEqual([item.ico for item in default_result], ["10000001"])
        self.assertEqual([item.ico for item in trader_result], ["10000002"])
        self.assertEqual({item.ico for item in all_result}, {"10000001", "10000002"})

    def test_navigation_and_heading_identify_sole_traders(self):
        response = self.app.test_client().get("/companies?entity=sole_trader")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Živnostníci z RPO".encode(), response.data)
        self.assertIn(b"J\xc3\xa1n Test", response.data)
        self.assertNotIn(b"Firma, s. r. o.", response.data)


if __name__ == "__main__":
    unittest.main()
