import unittest

from app import create_app
from extensions import db
from models import (
    Campaign,
    CampaignRecipient,
    Company,
    CompanyContact,
    Lead,
    OutboundEmail,
    PostalLocation,
    Suppression,
)
from services.campaigns import campaign_recipient_for_message_ids
from services.company_filtering import (
    company_filters_from_source,
    filtered_companies_query,
)
from services.postal_locations import parse_geonames_postal_text


class CampaignWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "SQLALCHEMY_DATABASE_URI": "sqlite://",
                "WTF_CSRF_ENABLED": False,
                "MAIL_SUPPRESS_SEND": True,
                "MAIL_DEFAULT_SENDER": "sender@example.test",
            }
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def add_company(self, name, ico, postal_code, email=None, nace="4321"):
        company = Company(
            official_name=name,
            ico=ico,
            postal_code=postal_code,
            municipality=name.split()[0],
            sk_nace_code=nace,
        )
        db.session.add(company)
        db.session.flush()
        if email:
            db.session.add(
                CompanyContact(
                    company=company,
                    contact_type="email",
                    value=email,
                    source_type="test",
                    confidence_score=90,
                )
            )
        return company

    def add_location(self, postal_code, place_name, latitude, longitude):
        db.session.add(
            PostalLocation(
                postal_code=postal_code,
                place_name=place_name,
                search_name=place_name.casefold(),
                admin_name_2="Poprad" if place_name != "Košice" else "Košice",
                latitude=latitude,
                longitude=longitude,
                source="test",
            )
        )

    def test_radius_filter_uses_cached_postal_coordinates(self):
        poprad = self.add_company("Poprad Firma", "10000001", "05801")
        svit = self.add_company("Svit Firma", "10000002", "05921")
        self.add_company("Košice Firma", "10000003", "04001")
        self.add_location("05801", "Poprad", 49.055, 20.305)
        self.add_location("05921", "Svit", 49.061, 20.206)
        self.add_location("04001", "Košice", 48.716, 21.261)
        db.session.commit()

        filters = company_filters_from_source(
            {"near": "Poprad", "radius_km": "20"}
        )
        result = filtered_companies_query(filters).order_by(Company.id).all()

        self.assertEqual([company.id for company in result], [poprad.id, svit.id])

    def test_filtered_companies_become_drafts_and_suppressions_are_skipped(self):
        allowed = self.add_company(
            "Dobrá Firma",
            "20000001",
            "05801",
            "kontakt@dobra.test",
        )
        self.add_company(
            "Potlačená Firma",
            "20000002",
            "05801",
            "stop@potlacena.test",
        )
        campaign = Campaign(
            name="Test kampane",
            offer_type="service",
            offer_description="Automatizácia objednávok",
            subject_template="Otázka pre {company_name}",
            body_template="Dobrý deň, {company_name} z obce {municipality}.",
            daily_limit=10,
        )
        db.session.add_all(
            [
                campaign,
                Suppression(
                    scope="email",
                    value="stop@potlacena.test",
                    reason="test",
                ),
            ]
        )
        db.session.commit()

        response = self.client.post(
            f"/campaigns/{campaign.id}/add-filtered",
            data={"sk_nace": "4321", "max_recipients": "25"},
        )

        self.assertEqual(response.status_code, 302)
        recipients = CampaignRecipient.query.all()
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0].company_id, allowed.id)
        self.assertEqual(recipients[0].status, "draft")
        self.assertIn("Dobrá Firma", recipients[0].subject)

    def test_daily_limit_sends_only_approved_recipient_and_links_history(self):
        first = self.add_company(
            "Prvá Firma",
            "30000001",
            "05801",
            "prva@example.test",
        )
        second = self.add_company(
            "Druhá Firma",
            "30000002",
            "05801",
            "druha@example.test",
        )
        campaign = Campaign(
            name="Limit jedna",
            offer_type="collaboration",
            offer_description="Spolupráca",
            subject_template="Predmet",
            body_template="Text",
            daily_limit=1,
        )
        db.session.add(campaign)
        db.session.flush()
        contacts = {
            contact.company_id: contact
            for contact in CompanyContact.query.filter(
                CompanyContact.company_id.in_([first.id, second.id])
            ).all()
        }
        recipients = [
            CampaignRecipient(
                campaign=campaign,
                company=company,
                contact=contacts[company.id],
                recipient_email=contacts[company.id].value,
                subject="Predmet",
                body="Text",
                status="approved",
            )
            for company in (first, second)
        ]
        db.session.add_all(recipients)
        db.session.commit()

        response = self.client.post(
            f"/campaigns/{campaign.id}/send",
            data={"batch_size": "2"},
        )

        self.assertEqual(response.status_code, 302)
        states = [recipient.status for recipient in CampaignRecipient.query.order_by(CampaignRecipient.id)]
        self.assertEqual(states, ["sent", "approved"])
        outbound = OutboundEmail.query.one()
        self.assertEqual(outbound.campaign_recipient_id, recipients[0].id)
        self.assertEqual(Lead.query.count(), 1)
        self.assertEqual(
            campaign_recipient_for_message_ids([outbound.message_id]).id,
            recipients[0].id,
        )
        self.assertEqual(
            self.client.get(f"/campaigns/{campaign.id}").status_code,
            200,
        )

    def test_campaign_can_be_created_from_form(self):
        response = self.client.post(
            "/campaigns/new",
            data={
                "name": "Produkt pre stolárov",
                "offer_type": "product",
                "offer_description": "Jednoduché spracovanie dopytov",
                "subject_template": "Otázka pre {company_name}",
                "body_template": "Dobrý deň, rád by som ukázal krátke demo.",
                "daily_limit": "10",
            },
        )

        self.assertEqual(response.status_code, 302)
        campaign = Campaign.query.one()
        self.assertEqual(campaign.daily_limit, 10)
        self.assertEqual(
            self.client.get(f"/campaigns/{campaign.id}").status_code,
            200,
        )

    def test_geonames_parser_normalizes_postal_code(self):
        rows = parse_geonames_postal_text(
            "SK\t058 01\tPoprad\tPrešovský kraj\tPV\tPoprad\t706\t\t\t49.055\t20.305\t4"
        )

        self.assertEqual(rows[0]["postal_code"], "05801")
        self.assertEqual(rows[0]["search_name"], "poprad")


if __name__ == "__main__":
    unittest.main()
