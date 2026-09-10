import gzip
import io
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask

from extensions import db
from models import Company, CompanyContact, CompanySource, SyncState
from services.rpo_sync import (
    RPO_COMPANY_EXPORT_SYNC_NAME,
    RPO_SYNC_URL,
    RPO_SOLE_TRADER_EXPORT_SYNC_NAME,
    SYNC_NAME,
    companies_for_contact_enrichment,
    create_initial_sync_url,
    find_rpo_source,
    import_rpo_companies_batch,
    import_rpo_sole_traders_batch,
    import_rpo_sole_traders_export,
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


class ExportBytesIO(io.BytesIO):
    pass


class FakeExportResponse:
    def __init__(self, records):
        payload = (
            '{"exportDate":"2026-09-05","results":[\n'
            + "\n".join(
                ("" if index == 0 else ",")
                + json.dumps(record, ensure_ascii=False)
                for index, record in enumerate(records)
            )
            + "\n]}"
        )
        self.raw = ExportBytesIO(gzip.compress(payload.encode("utf-8")))
        self.ok = True
        self.status_code = 200

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

    def test_full_sync_ignores_last_successful_checkpoint(self):
        state = SyncState(
            name=SYNC_NAME,
            last_successful_sync_at=datetime(
                2026,
                8,
                20,
                tzinfo=timezone.utc,
            ),
        )

        incremental_url = create_initial_sync_url(
            state,
            only_ids=False,
        )
        full_url = create_initial_sync_url(
            state,
            only_ids=False,
            full_sync=True,
        )

        self.assertIn("since=", incremental_url)
        self.assertEqual(full_url, RPO_SYNC_URL)

    @patch("services.rpo_sync.build_http_session")
    def test_official_export_import_selects_only_active_sole_traders(
        self,
        build_session,
    ):
        active_trader = {
            "id": 1001,
            "identifiers": [{"value": "12345678"}],
            "fullNames": [{"value": "Ján Živnostník"}],
            "statisticalCodes": {
                "mainActivity": {
                    "code": "4321",
                    "value": "Elektroinštalačné práce",
                }
            },
            "legalForms": [
                {
                    "value": {
                        "value": (
                            "Podnikateľ-fyzická osoba-nezapísaný "
                            "v obchodnom registri"
                        )
                    }
                }
            ],
            "sourceRegister": {
                "value": {"value": "Živnostenský register"}
            },
        }
        terminated_trader = {
            **active_trader,
            "id": 1002,
            "identifiers": [{"value": "12345679"}],
            "termination": "2025-01-01",
        }
        company = {
            **active_trader,
            "id": 1003,
            "identifiers": [{"value": "12345670"}],
            "sourceRegister": {"value": {"value": "Obchodný register"}},
        }
        unrelated_trader = {
            **active_trader,
            "id": 1004,
            "identifiers": [{"value": "12345671"}],
            "statisticalCodes": {
                "mainActivity": {
                    "code": "5611",
                    "value": "Reštauračné činnosti",
                }
            },
        }
        response = FakeExportResponse(
            [active_trader, terminated_trader, company, unrelated_trader]
        )
        session = SimpleNamespace(
            get=Mock(return_value=response),
            close=Mock(),
        )
        build_session.return_value = session

        result = import_rpo_sole_traders_export(
            batch_date="2026-09-05",
            file_number=3,
            max_records=10,
            commit_every=1,
        )

        self.assertEqual(result["scanned"], 4)
        self.assertEqual(result["active_sole_traders_scanned"], 2)
        self.assertEqual(result["skipped_outside_target_focus"], 1)
        self.assertEqual(result["selected_active_sole_traders"], 1)
        self.assertEqual(result["upserted"], 1)
        self.assertEqual(Company.query.count(), 1)
        self.assertEqual(Company.query.one().ico, "12345678")
        session.get.assert_called_once_with(
            result["source_url"],
            timeout=(10, 180),
            allow_redirects=False,
            stream=True,
        )
        session.close.assert_called_once()

    @patch("services.rpo_sync.build_http_session")
    def test_batch_import_commits_stop_checkpoint_and_resumes(
        self,
        build_session,
    ):
        def trader(record_id, ico):
            return {
                "id": record_id,
                "identifiers": [{"value": ico}],
                "fullNames": [{"value": f"Živnostník {record_id}"}],
                "legalForms": [
                    {
                        "value": {
                            "value": (
                                "Podnikateľ-fyzická osoba-nezapísaný "
                                "v obchodnom registri"
                            )
                        }
                    }
                ],
                "sourceRegister": {
                    "value": {"value": "Živnostenský register"}
                },
                "statisticalCodes": {
                    "mainActivity": {"code": "4321"}
                },
            }

        first_file_records = [
            trader(2001, "20000001"),
            trader(2002, "20000002"),
        ]
        second_file_records = [trader(2003, "20000003")]
        first_session = SimpleNamespace(
            get=Mock(return_value=FakeExportResponse(first_file_records)),
            close=Mock(),
        )
        resumed_session = SimpleNamespace(
            get=Mock(
                side_effect=[
                    FakeExportResponse(first_file_records),
                    FakeExportResponse(second_file_records),
                ]
            ),
            close=Mock(),
        )
        build_session.side_effect = [first_session, resumed_session]

        stop_checks = 0

        def stop_after_first_record():
            nonlocal stop_checks
            stop_checks += 1
            return stop_checks >= 2

        stopped = import_rpo_sole_traders_batch(
            batch_date="2026-09-05",
            first_file=1,
            last_file=2,
            commit_every=100,
            should_stop=stop_after_first_record,
        )

        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["imported"], 1)
        self.assertEqual(stopped["checkpoint"]["file_number"], 1)
        self.assertEqual(stopped["checkpoint"]["record_offset"], 1)
        self.assertEqual(Company.query.count(), 1)

        resumed = import_rpo_sole_traders_batch(
            batch_date="2026-09-05",
            first_file=1,
            last_file=2,
            commit_every=100,
        )

        self.assertEqual(resumed["status"], "success")
        self.assertEqual(resumed["fetched"], 3)
        self.assertEqual(resumed["imported"], 3)
        self.assertTrue(resumed["checkpoint"]["completed"])
        self.assertEqual(Company.query.count(), 3)
        state = SyncState.query.filter_by(
            name=RPO_SOLE_TRADER_EXPORT_SYNC_NAME
        ).one()
        self.assertEqual(state.status, "success")
        self.assertEqual(state.processed_records, 3)
        first_session.close.assert_called_once()
        resumed_session.close.assert_called_once()

        repeated = import_rpo_sole_traders_batch(
            batch_date="2026-09-05",
            first_file=1,
            last_file=2,
        )
        self.assertTrue(repeated["already_completed"])
        self.assertEqual(build_session.call_count, 2)

    @patch("services.rpo_sync.build_http_session")
    def test_batch_import_stops_at_record_limit_and_can_continue(
        self,
        build_session,
    ):
        def trader(record_id, ico):
            return {
                "id": record_id,
                "identifiers": [{"value": ico}],
                "fullNames": [{"value": f"Živnostník {record_id}"}],
                "legalForms": [
                    {
                        "value": {
                            "value": (
                                "Podnikateľ-fyzická osoba-nezapísaný "
                                "v obchodnom registri"
                            )
                        }
                    }
                ],
                "sourceRegister": {
                    "value": {"value": "Živnostenský register"}
                },
                "statisticalCodes": {
                    "mainActivity": {"code": "4321"}
                },
            }

        records = [
            trader(3001, "30000001"),
            trader(3002, "30000002"),
            trader(3003, "30000003"),
        ]
        first_session = SimpleNamespace(
            get=Mock(return_value=FakeExportResponse(records)),
            close=Mock(),
        )
        resumed_session = SimpleNamespace(
            get=Mock(return_value=FakeExportResponse(records)),
            close=Mock(),
        )
        build_session.side_effect = [first_session, resumed_session]

        limited = import_rpo_sole_traders_batch(
            batch_date="2026-09-05",
            first_file=1,
            last_file=1,
            commit_every=100,
            max_records=2,
        )

        self.assertEqual(limited["status"], "partial")
        self.assertEqual(limited["imported"], 2)
        self.assertEqual(limited["checkpoint"]["record_offset"], 2)
        self.assertEqual(Company.query.count(), 2)

        resumed = import_rpo_sole_traders_batch(
            batch_date="2026-09-05",
            first_file=1,
            last_file=1,
            commit_every=100,
            max_records=3,
        )

        self.assertEqual(resumed["status"], "partial")
        self.assertEqual(resumed["imported"], 3)
        self.assertEqual(resumed["checkpoint"]["record_offset"], 3)
        self.assertEqual(Company.query.count(), 3)
        self.assertEqual(build_session.call_count, 2)

    @patch("services.rpo_sync.build_http_session")
    def test_company_batch_import_uses_separate_checkpoint_and_only_active_sro(
        self,
        build_session,
    ):
        def organization(record_id, ico, legal_form, register, termination=None):
            return {
                "id": record_id,
                "identifiers": [{"value": ico}],
                "fullNames": [{"value": f"Subjekt {record_id}"}],
                "legalForms": [{"value": {"value": legal_form}}],
                "sourceRegister": {"value": {"value": register}},
                "termination": termination,
            }

        records = [
            organization(
                4001,
                "40000001",
                "Spoločnosť s ručením obmedzeným",
                "Obchodný register",
            ),
            organization(
                4002,
                "40000002",
                "Spoločnosť s ručením obmedzeným",
                "Obchodný register",
                termination="2025-01-01",
            ),
            organization(
                4003,
                "40000003",
                "Podnikateľ-fyzická osoba-nezapísaný v obchodnom registri",
                "Živnostenský register",
            ),
            organization(
                4004,
                "40000004",
                "Akciová spoločnosť",
                "Obchodný register",
            ),
        ]
        session = SimpleNamespace(
            get=Mock(return_value=FakeExportResponse(records)),
            close=Mock(),
        )
        build_session.return_value = session

        result = import_rpo_companies_batch(
            batch_date="2026-09-05",
            first_file=1,
            last_file=1,
            commit_every=2,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["fetched"], 4)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 3)
        self.assertTrue(result["checkpoint"]["completed"])
        self.assertEqual(Company.query.count(), 1)
        self.assertEqual(Company.query.one().ico, "40000001")
        company_state = SyncState.query.filter_by(
            name=RPO_COMPANY_EXPORT_SYNC_NAME
        ).one()
        self.assertEqual(company_state.status, "success")
        self.assertEqual(company_state.processed_records, 1)
        self.assertIsNone(
            SyncState.query.filter_by(name=RPO_SOLE_TRADER_EXPORT_SYNC_NAME).one_or_none()
        )
        session.close.assert_called_once()

    def test_rpo_source_lookup_filters_same_external_id_by_source_type(self):
        company = Company(ico="12345678", official_name="Test")
        db.session.add(company)
        db.session.flush()
        db.session.add_all(
            [
                CompanySource(
                    company_id=company.id,
                    source_type="manual",
                    source_id="same-id-manual",
                    external_id="1001",
                    raw_data={},
                ),
                CompanySource(
                    company_id=company.id,
                    source_type="rpo2",
                    source_id="same-id-rpo",
                    external_id="1001",
                    raw_data={},
                ),
            ]
        )
        db.session.commit()

        source = find_rpo_source(1001)

        self.assertIsNotNone(source)
        self.assertEqual(source.source_type, "rpo2")


if __name__ == "__main__":
    unittest.main()
