import unittest
from datetime import datetime, timezone

from app import create_app
from extensions import db
from models import Campaign, Lead, LeadActivity, Opportunity, OutboundEmail


class OpportunityConversionTests(unittest.TestCase):
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
        self.client = self.app.test_client()

        self.campaign = Campaign(
            name="Stavebné zákazky",
            offer_type="service",
            offer_description="Stavebné práce",
            subject_template="Spolupráca pre {company_name}",
            body_template="Ponúkame kapacitu pre {company_name} v lokalite {municipality}.",
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
            discovered_at=datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc),
            last_seen_at=datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc),
        )
        db.session.add(self.opportunity)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    @property
    def convert_url(self):
        return (
            f"/campaigns/{self.campaign.id}/opportunities/"
            f"{self.opportunity.id}/convert"
        )

    def valid_form(self):
        return {
            "verification_confirmed": "on",
            "company_name": "Overená stavebná firma",
            "verification_note": "Dopyt aj firemný kontakt overené na zdroji 4. 9. 2026.",
            "contact_source_url": "https://example.sk/kontakt?utm_source=test",
            "email": "Zakazky@Example.sk",
            "phone": "+421 900 123 456",
            "website": "https://example.sk",
            "city": "Poprad",
        }

    def test_campaign_detail_shows_manual_conversion_without_send_action(self):
        response = self.client.get(f"/campaigns/{self.campaign.id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Ručne overiť".encode(), response.data)
        self.assertIn("nič neodošle".encode(), response.data)
        self.assertNotIn("Otvoriť CRM lead".encode(), response.data)

    def test_manual_confirmation_is_required(self):
        form = self.valid_form()
        form.pop("verification_confirmed")

        response = self.client.post(self.convert_url, data=form)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Lead.query.count(), 0)
        self.assertEqual(OutboundEmail.query.count(), 0)
        self.assertEqual(db.session.get(Opportunity, self.opportunity.id).status, "new")

    def test_verified_opportunity_creates_reviewable_lead_without_sending(self):
        response = self.client.post(self.convert_url, data=self.valid_form())

        self.assertEqual(response.status_code, 302)
        lead = Lead.query.one()
        opportunity = db.session.get(Opportunity, self.opportunity.id)
        self.assertTrue(response.location.endswith(f"/lead/{lead.id}"))
        self.assertEqual(opportunity.lead_id, lead.id)
        self.assertEqual(opportunity.status, "converted")
        self.assertIsNotNone(opportunity.verified_at)
        self.assertIsNotNone(opportunity.converted_at)
        self.assertEqual(opportunity.contact_source_url, "https://example.sk/kontakt")
        self.assertEqual(lead.company_name, "Overená stavebná firma")
        self.assertEqual(lead.email, "zakazky@example.sk")
        self.assertEqual(lead.status, "Osloviť")
        self.assertEqual(lead.work_type, "Stavebné práce")
        self.assertEqual(lead.lead_score, 4)
        self.assertEqual(lead.suggested_subject, "Spolupráca pre Overená stavebná firma")
        self.assertIn("Overená stavebná firma", lead.suggested_message)
        self.assertIn("Poprad", lead.suggested_message)
        self.assertIn("neposielať", lead.suggested_message)
        self.assertEqual(OutboundEmail.query.count(), 0)
        self.assertEqual(LeadActivity.query.count(), 1)
        self.assertIn("nič nebolo odoslané", LeadActivity.query.one().note)
        detail = self.client.get(f"/lead/{lead.id}")
        self.assertIn(lead.suggested_subject.encode(), detail.data)

    def test_repeated_conversion_reuses_existing_lead(self):
        first = self.client.post(self.convert_url, data=self.valid_form())
        second = self.client.post(self.convert_url, data=self.valid_form())

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(Lead.query.count(), 1)
        self.assertEqual(LeadActivity.query.count(), 1)
        self.assertEqual(OutboundEmail.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
