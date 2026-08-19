import unittest

from app import create_app
from extensions import db
from models import Company, CompanyActivity, CompanySource
from services.rpo_sync import upsert_company


RECORD = {
    "id": 18075205,
    "data": {
        "fullNames": [
            {
                "value": "Firma ABC s.r.o.",
                "validFrom": "2020-01-01",
            }
        ],
        "identifiers": [
            {
                "value": "12345678",
                "validFrom": "2020-01-01",
            }
        ],
        "legalForms": [
            {
                "value": "Spoločnosť s ručením obmedzeným",
                "validFrom": "2020-01-01",
            }
        ],
        "activities": [
            {
                "economicActivityDescription": "Elektroinštalácie",
                "validFrom": "2020-01-01",
            },
            {
                "economicActivityDescription": "Montáž rozvádzačov",
                "validFrom": "2022-05-01",
            },
        ],
    },
}


class CompanyUpsertTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            {
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": "sqlite://",
                "WTF_CSRF_ENABLED": False,
            }
        )
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_upsert_is_idempotent(self):
        upsert_company(RECORD)
        db.session.commit()
        upsert_company(RECORD)
        db.session.commit()

        self.assertEqual(Company.query.count(), 1)
        self.assertEqual(CompanySource.query.count(), 1)
        self.assertEqual(CompanyActivity.query.count(), 2)


if __name__ == "__main__":
    unittest.main()
