import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from flask import Flask

from extensions import db
from models import Company, CompanyContact, SyncState
from services.rpo_sync import (
    SYNC_NAME,
    companies_for_contact_enrichment,
    sync_rpo,
)


class FakeResponse:
    def __init__(self, records):
        self._records = records

    def json(self):
        return self._records


class FakeSession:
    def close(self):
        pass


class RpoSyncCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    @patch("services.rpo_sync.time.sleep")
    @patch("services.rpo_sync.upsert_rpo_record")
    @patch("services.rpo_sync.extract_next_url")
    @patch("services.rpo_sync.request_page")
    @patch("services.rpo_sync.build_http_session")
    def test_limit_checkpoints_only_after_complete_page(
        self,
        build_session,
        request_page,
        extract_next_url,
        upsert_record,
        sleep,
    ):
        build_session.return_value = FakeSession()
        request_page.return_value = FakeResponse([{"id": 1}, {"id": 2}])
        extract_next_url.return_value = "https://example.test/page-2"
        upsert_record.side_effect = [object(), None]

        result = sync_rpo(max_records=1, delay_seconds=0)

        state = SyncState.query.filter_by(name=SYNC_NAME).one()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["fetched"], 2)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(state.next_url, "https://example.test/page-2")
        self.assertEqual(state.fetched_records, 2)
        self.assertEqual(state.processed_records, 1)
        self.assertEqual(state.skipped_records, 1)

        request_page.return_value = FakeResponse([{"id": 3}])
        extract_next_url.return_value = None
        upsert_record.side_effect = [object()]

        resumed_result = sync_rpo(max_records=10, delay_seconds=0)

        state = SyncState.query.filter_by(name=SYNC_NAME).one()
        self.assertEqual(resumed_result["status"], "success")
        self.assertEqual(resumed_result["fetched"], 1)
        self.assertEqual(resumed_result["imported"], 1)
        self.assertIsNone(state.next_url)
        self.assertEqual(state.fetched_records, 3)
        self.assertEqual(state.processed_records, 2)
        self.assertEqual(state.skipped_records, 1)

    def test_contact_enrichment_skips_checked_and_existing_contacts(self):
        never_checked = Company(ico="10000001", official_name="Nová firma")
        checked_without_contact = Company(
            ico="10000002",
            official_name="Skontrolovaná firma",
            contacts_checked_at=datetime.now(timezone.utc),
        )
        existing_contact = Company(
            ico="10000003",
            official_name="Firma s kontaktom",
        )
        db.session.add_all(
            [never_checked, checked_without_contact, existing_contact]
        )
        db.session.flush()
        db.session.add(
            CompanyContact(
                company_id=existing_contact.id,
                contact_type="email",
                value="info@example.sk",
                source_type="test",
            )
        )
        db.session.commit()

        default_companies = companies_for_contact_enrichment(
            include_existing=False
        )
        all_companies = companies_for_contact_enrichment(include_existing=True)

        self.assertEqual(
            [company.ico for company in default_companies],
            ["10000001"],
        )
        self.assertEqual(len(all_companies), 3)


if __name__ == "__main__":
    unittest.main()
