import unittest
from unittest.mock import patch

from app import create_app
from extensions import db
from models import Campaign, CampaignRecipient, Company, CompanyContact, Lead
from services.hubspot import sync_lead_to_hubspot


class IntegrationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "SQLALCHEMY_DATABASE_URI": "sqlite://",
                "WTF_CSRF_ENABLED": False,
                "MAIL_SUPPRESS_SEND": True,
                "MAIL_DEFAULT_SENDER": "sender@example.test",
                "WORK_API_TOKEN": "work-test-token",
                "HUBSPOT_ACCESS_TOKEN": "hubspot-test-token",
            }
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()

        self.company = Company(
            official_name="Elektro Test, s. r. o.",
            ico="12345678",
            sk_nace_code="43210",
            municipality="Poprad",
        )
        self.email = CompanyContact(
            company=self.company,
            contact_type="email",
            value="info@example.test",
            source_type="test",
            is_verified=True,
        )
        self.website = CompanyContact(
            company=self.company,
            contact_type="website",
            value="https://www.example.test",
            source_type="test",
            is_verified=True,
        )
        self.campaign = Campaign(
            name="MVP test",
            offer_type="collaboration",
            offer_description="Subcontracting capacity",
            subject_template="Test",
            body_template="Test body",
        )
        db.session.add_all([self.company, self.email, self.website, self.campaign])
        db.session.flush()
        self.recipient = CampaignRecipient(
            campaign=self.campaign,
            company=self.company,
            contact=self.email,
            recipient_email=self.email.value,
            subject="Draft subject",
            body="Draft body",
        )
        db.session.add(self.recipient)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_work_batch_requires_token_and_returns_drafts(self):
        path = f"/integrations/work/campaigns/{self.campaign.id}/batch"
        self.assertEqual(self.client.get(path).status_code, 401)

        response = self.client.get(path, headers={"X-Work-Token": "work-test-token"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["recipients"][0]["recipient_id"], self.recipient.id)
        self.assertEqual(response.json["recipients"][0]["status"], "draft")

    def test_work_results_update_score_but_never_approve(self):
        response = self.client.post(
            f"/integrations/work/campaigns/{self.campaign.id}/results",
            headers={"X-Work-Token": "work-test-token"},
            json={
                "results": [{
                    "recipient_id": self.recipient.id,
                    "fit_score": 82,
                    "fit_reason": "Relevant electrical contractor.",
                    "analysis": {
                        "company_type": "Electrical contractor",
                        "services": ["Industrial installations"],
                        "markets": ["Slovakia"],
                        "works_abroad": False,
                        "regions": ["Prešov"],
                        "employee_count": None,
                        "employee_count_source": None,
                        "subcontractor_need": "medium",
                        "outreach_relevant": True,
                        "analysis_reason": "Relevant public evidence.",
                        "analysis_evidence": [{
                            "quote": "Industrial electrical installations",
                            "source_url": "https://example.test/services",
                        }],
                    },
                }]
            },
        )

        self.assertEqual(response.status_code, 200)
        db.session.refresh(self.recipient)
        self.assertEqual(self.recipient.fit_score, 82)
        self.assertEqual(self.recipient.selection_source, "chatgpt_work")
        self.assertEqual(self.recipient.status, "draft")
        self.assertTrue(self.company.outreach_relevant)

    @patch("services.hubspot._request")
    def test_hubspot_sync_upserts_and_persists_ids(self, request_mock):
        lead = Lead(
            company=self.company,
            company_name=self.company.official_name,
            email=self.email.value,
        )
        db.session.add(lead)
        db.session.commit()
        request_mock.side_effect = [
            None,
            {"id": "contact-1"},
            None,
            {"results": []},
            {"id": "company-1"},
            {},
            {"id": "note-1"},
            {},
            {},
        ]

        result = sync_lead_to_hubspot(lead, note_body="Reply received")

        self.assertEqual(result["contact_id"], "contact-1")
        self.assertEqual(lead.hubspot_company_id, "company-1")
        self.assertEqual(lead.hubspot_contact_id, "contact-1")
        self.assertIsNotNone(lead.hubspot_synced_at)
        self.assertEqual(request_mock.call_count, 9)

    @patch("campaign_routes.sync_lead_to_hubspot")
    def test_hubspot_route_runs_only_on_explicit_post(self, sync_mock):
        response = self.client.post(
            f"/campaigns/{self.campaign.id}/recipients/{self.recipient.id}/sync-hubspot"
        )

        self.assertEqual(response.status_code, 302)
        sync_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
