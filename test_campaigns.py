import unittest
from unittest.mock import patch

from app import create_app
from extensions import db
from models import (
    Campaign,
    CampaignRecipient,
    Company,
    CompanyContact,
    Lead,
    LandingPage,
    OutboundEmail,
    PostalLocation,
    Suppression,
)
from services.campaigns import (
    campaign_recipient_for_message_ids,
    clean_automated_outreach_body,
)
from services.campaign_automation import run_campaign_automation
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
                "PUBLIC_BASE_URL": "https://offers.example.test",
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

    def add_company(
        self,
        name,
        ico,
        postal_code,
        email=None,
        nace="4321",
        municipality=None,
    ):
        company = Company(
            official_name=name,
            ico=ico,
            postal_code=postal_code,
            municipality=municipality or name.split()[0],
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
                    is_verified=True,
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

    def test_ai_outreach_cleaner_removes_greeting_company_and_questions(self):
        body = (
            "Dobrý deň, Firma Alpha, ponúkame jednoduchšie spracovanie zákaziek. "
            "Má význam poslať viac informácií?\n\nS pozdravom\nAdam"
        )

        cleaned = clean_automated_outreach_body(body, "Firma Alpha")

        self.assertEqual(
            cleaned,
            "ponúkame jednoduchšie spracovanie zákaziek.\n\nS pozdravom\nAdam",
        )
        self.assertNotIn("?", cleaned)

        shortened_name = clean_automated_outreach_body(
            "Dobrý deň, Firma Alpha, prevádzka Bratislava, pripravujeme riešenie. "
            "Radi by sme zistili, či vám pomôže.\n\nS pozdravom\nAdam",
            "Firma Alpha s.r.o.",
        )
        self.assertEqual(
            shortened_name,
            "pripravujeme riešenie.\n\nS pozdravom\nAdam",
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
            "kontakt@dobra.example.com",
        )
        self.add_company(
            "Potlačená Firma",
            "20000002",
            "05801",
            "stop@potlacena.example.com",
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
                    value="stop@potlacena.example.com",
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
            "prva@example.com",
        )
        second = self.add_company(
            "Druhá Firma",
            "30000002",
            "05801",
            "druha@example.com",
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

    def test_campaign_can_store_disabled_scout_configuration(self):
        response = self.client.post(
            "/campaigns/new",
            data={
                "name": "Elektro zákazky",
                "offer_type": "service",
                "offer_description": "Elektroinštalácie a subdodávky",
                "subject_template": "Ponuka elektro spolupráce",
                "body_template": "Dobrý deň, ponúkam elektro práce.",
                "business_line": "electrical",
                "scout_queries": "elektro subdodávateľ Slovensko\nRFQ electrical Slovakia",
            },
        )

        self.assertEqual(response.status_code, 302)
        campaign = Campaign.query.one()
        self.assertEqual(campaign.business_line, "electrical")
        self.assertFalse(campaign.scout_enabled)
        self.assertEqual(
            campaign.scout_queries,
            ["elektro subdodávateľ Slovensko", "RFQ electrical Slovakia"],
        )

    def test_enabled_scout_requires_a_query(self):
        response = self.client.post(
            "/campaigns/new",
            data={
                "name": "Stavebné zákazky",
                "offer_type": "service",
                "offer_description": "Stavebné práce",
                "subject_template": "Ponuka",
                "body_template": "Dobrý deň.",
                "business_line": "construction",
                "scout_enabled": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Campaign.query.count(), 0)
        self.assertIn("aspoň jeden vyhľadávací dotaz".encode(), response.data)

    @patch("campaign_routes.generate_campaign_plan")
    def test_automated_campaign_can_generate_targeting_from_product(self, generate_plan):
        generate_plan.return_value = {
            "targeting_profile": {
                "ideal_customer_profile": "Autoservisy prijímajúce objednávky telefonicky",
                "nace_keywords": ["45.20"],
                "company_keywords": ["autoservis"],
                "selection_signals": ["oprava vozidiel"],
                "exclusion_signals": [],
                "minimum_fit_score": 60,
            },
            "subject_template": "Telefonické objednávky pre {company_name}",
            "body_template": "Dobrý deň, ponúkame nástroj pre autoservisy.",
        }

        response = self.client.post(
            "/campaigns/new",
            data={
                "name": "AI recepčný – test",
                "offer_type": "product",
                "offer_stage": "validation",
                "offer_description": "AI recepčný pre autoservisy",
                "automation_enabled": "on",
                "target_hint": "menšie autoservisy",
                "target_total": "50",
                "batch_size": "10",
                "daily_limit": "10",
                "contact_cooldown_days": "90",
            },
        )

        self.assertEqual(response.status_code, 302)
        campaign = Campaign.query.one()
        self.assertTrue(campaign.automation_enabled)
        self.assertEqual(campaign.offer_stage, "validation")
        self.assertEqual(campaign.target_total, 50)
        self.assertEqual(campaign.batch_size, 10)
        self.assertEqual(campaign.status, "draft")
        self.assertEqual(campaign.targeting_profile["nace_keywords"], ["45.20"])
        self.assertIn("nejde ešte o hotový produkt", campaign.body_template)
        self.assertEqual(
            generate_plan.call_args.kwargs["campaign_name"],
            "AI recepčný – test",
        )
        self.assertEqual(
            self.client.get(f"/campaigns/{campaign.id}").status_code,
            200,
        )

    @patch("campaign_routes.generate_campaign_plan")
    def test_targeting_can_be_regenerated_from_campaign_name(self, generate_plan):
        campaign = Campaign(
            name="Stolári Bratislava",
            offer_type="product",
            offer_stage="ready",
            offer_description="Spracovanie zákaziek z WhatsAppu",
            subject_template="Otázka",
            body_template="Ponuka",
            status="draft",
            automation_enabled=True,
            targeting_profile={
                "ideal_customer_profile": "Všeobecné firmy",
                "nace_keywords": ["82.99"],
                "company_keywords": ["administratíva"],
                "minimum_fit_score": 60,
                "target_hint": "",
            },
            target_total=10,
            batch_size=5,
            daily_limit=5,
        )
        db.session.add(campaign)
        db.session.commit()
        generate_plan.return_value = {
            "targeting_profile": {
                "ideal_customer_profile": "Stolári v Bratislave",
                "nace_keywords": ["43.32", "31.00", "16.23"],
                "company_keywords": ["stolárstvo", "nábytok"],
                "location_keywords": ["Bratislava"],
                "selection_signals": [],
                "exclusion_signals": [],
                "minimum_fit_score": 60,
                "target_hint": "Stolári Bratislava",
            },
            "subject_template": "Nový predmet",
            "body_template": "Nový text",
        }

        response = self.client.post(
            f"/campaigns/{campaign.id}/regenerate-targeting"
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            campaign.targeting_profile["nace_keywords"],
            ["43.32", "31.00", "16.23"],
        )
        self.assertEqual(
            campaign.targeting_profile["location_keywords"],
            ["Bratislava"],
        )
        self.assertEqual(campaign.subject_template, "Otázka")
        self.assertEqual(
            generate_plan.call_args.kwargs["target_hint"],
            "Stolári Bratislava",
        )

    @patch("services.campaign_automation.rank_campaign_candidates")
    def test_button_selects_draft_companies_from_nace_and_activation_approves_them(
        self,
        rank,
    ):
        first = self.add_company(
            "Firma Alpha",
            "41000001",
            "05801",
            "alpha@example.com",
            nace="4520",
            municipality="Bratislava",
        )
        second = self.add_company(
            "Firma Beta",
            "41000002",
            "05801",
            "beta@example.com",
            nace="4520",
            municipality="Bratislava - Ružinov",
        )
        outside = self.add_company(
            "Firma Mimo",
            "41000003",
            "04001",
            "mimo@example.com",
            nace="4520",
            municipality="Košice",
        )
        campaign = Campaign(
            name="Výber zo SK NACE",
            offer_type="product",
            offer_stage="ready",
            offer_description="Nástroj pre autoservisy",
            subject_template="Otázka",
            body_template="Ponuka",
            status="draft",
            automation_enabled=True,
            targeting_profile={
                "ideal_customer_profile": "Autoservisy",
                "nace_keywords": ["45.20"],
                "company_keywords": [],
                "location_keywords": ["Bratislava"],
                "selection_signals": [],
                "exclusion_signals": [],
                "minimum_fit_score": 60,
            },
            target_total=5,
            batch_size=2,
            daily_limit=2,
            follow_up_days=7,
            contact_cooldown_days=0,
        )
        db.session.add(campaign)
        db.session.commit()
        rank.return_value = [
            {
                "company_id": first.id,
                "fit_score": 90,
                "fit_reason": "SK NACE 45.20",
                "subject": "Prvá ponuka",
                "body": "Dobrý deň, stručná ponuka.",
            },
            {
                "company_id": second.id,
                "fit_score": 85,
                "fit_reason": "SK NACE 45.20",
                "subject": "Druhá ponuka",
                "body": "Dobrý deň, stručná ponuka.",
            },
        ]

        selected = self.client.post(
            f"/campaigns/{campaign.id}/select-companies",
            data={"selection_count": "2"},
        )

        self.assertEqual(selected.status_code, 302)
        recipients = CampaignRecipient.query.order_by(CampaignRecipient.id).all()
        self.assertEqual(len(recipients), 2)
        self.assertEqual([recipient.status for recipient in recipients], ["draft", "draft"])
        self.assertTrue(
            all(recipient.selection_source == "automation" for recipient in recipients)
        )
        ranked_company_ids = {
            company.id for company in rank.call_args.args[1]
        }
        self.assertEqual(ranked_company_ids, {first.id, second.id})
        self.assertNotIn(outside.id, ranked_company_ids)
        self.assertEqual(OutboundEmail.query.count(), 0)

        activated = self.client.post(
            f"/campaigns/{campaign.id}/status",
            data={"status": "active"},
        )

        self.assertEqual(activated.status_code, 302)
        self.assertEqual(
            [recipient.status for recipient in recipients],
            ["approved", "approved"],
        )
        self.assertTrue(all(recipient.approved_at for recipient in recipients))
        self.assertEqual(OutboundEmail.query.count(), 0)

    @patch("services.campaign_automation.rank_campaign_candidates")
    def test_automation_selects_small_batch_sends_and_creates_leads(self, rank):
        first = self.add_company(
            "Autoservis Prvý",
            "40000001",
            "05801",
            "prvy@example.com",
            nace="4520",
        )
        second = self.add_company(
            "Autoservis Druhý",
            "40000002",
            "05801",
            "druhy@example.com",
            nace="4520",
        )
        campaign = Campaign(
            name="Automatický test",
            offer_type="product",
            offer_stage="validation",
            offer_description="AI recepčný pre autoservisy",
            subject_template="Otázka pre {company_name}",
            body_template="Pripravujeme nástroj pre autoservisy.",
            status="active",
            automation_enabled=True,
            targeting_profile={
                "ideal_customer_profile": "Autoservisy",
                "nace_keywords": ["45.20"],
                "company_keywords": ["autoservis"],
                "selection_signals": [],
                "exclusion_signals": [],
                "minimum_fit_score": 60,
            },
            target_total=2,
            batch_size=2,
            daily_limit=2,
            follow_up_days=7,
            contact_cooldown_days=0,
        )
        db.session.add(campaign)
        db.session.commit()
        db.session.add(
            LandingPage(
                campaign=campaign,
                slug="ai-recepcny-autoservisy",
                preview_token="campaign-preview-token",
                status="published",
                content={},
                contact_email="adam@example.test",
            )
        )
        db.session.commit()
        rank.return_value = [
            {
                "company_id": first.id,
                "fit_score": 91,
                "fit_reason": "SK NACE autoservis",
                "subject": "Objednávky v Autoservis Prvý",
                "body": "Dobrý deň, ponúkame AI recepčného pre autoservisy. Má význam poslať viac informácií?\n\nS pozdravom\nAdam",
            },
            {
                "company_id": second.id,
                "fit_score": 88,
                "fit_reason": "SK NACE autoservis",
                "subject": "Objednávky v Autoservis Druhý",
                "body": "Dobrý deň, pripravujeme AI recepčného pre autoservisy. Má význam poslať viac informácií?\n\nS pozdravom\nAdam",
            },
        ]

        result = run_campaign_automation(campaign)

        self.assertEqual(result["prepared"]["prepared"], 2)
        self.assertEqual(result["delivery"]["sent"], 2)
        self.assertEqual(Lead.query.count(), 2)
        self.assertTrue(all(lead.next_follow_up_at for lead in Lead.query.all()))
        self.assertEqual(OutboundEmail.query.count(), 2)
        self.assertEqual(
            [recipient.fit_score for recipient in CampaignRecipient.query.order_by(CampaignRecipient.id)],
            [91, 88],
        )
        first_recipient = CampaignRecipient.query.order_by(CampaignRecipient.id).first()
        self.assertFalse(first_recipient.body.startswith("Dobrý deň"))
        self.assertNotIn("Má význam poslať viac informácií?", first_recipient.body)
        self.assertIn("nejde ešte o hotový produkt", first_recipient.body)
        self.assertIn(
            "https://offers.example.test/ponuka/ai-recepcny-autoservisy",
            first_recipient.body,
        )
        self.assertLess(
            first_recipient.body.index("Viac informácií o ponuke"),
            first_recipient.body.index("Ak si neželáte ďalšie správy"),
        )
        self.assertEqual(campaign.status, "completed")

        repeated = run_campaign_automation(campaign)
        self.assertIn("skipped", repeated)

    @patch("services.campaign_automation.enrich_company_contacts")
    @patch("services.campaign_automation.rank_campaign_candidates")
    def test_automation_enriches_selected_company_without_email(self, rank, enrich):
        company = self.add_company(
            "Autoservis Kontakt",
            "40000003",
            "05801",
            nace="4520",
        )
        campaign = Campaign(
            name="Enrichment test",
            offer_type="product",
            offer_stage="ready",
            offer_description="Rezervačný nástroj",
            subject_template="Predmet",
            body_template="Text",
            status="active",
            automation_enabled=True,
            targeting_profile={
                "ideal_customer_profile": "Autoservisy",
                "nace_keywords": ["4520"],
                "company_keywords": [],
                "minimum_fit_score": 60,
            },
            target_total=1,
            batch_size=1,
            daily_limit=1,
            follow_up_days=7,
            contact_cooldown_days=0,
        )
        db.session.add(campaign)
        db.session.commit()
        rank.return_value = [
            {
                "company_id": company.id,
                "fit_score": 85,
                "fit_reason": "Relevantná činnosť",
                "subject": "Rezervácie",
                "body": "Dobrý deň, ponúkame rezervačný nástroj.",
            }
        ]

        def add_email(**kwargs):
            selected = kwargs["companies"][0]
            db.session.add(
                CompanyContact(
                    company=selected,
                    contact_type="email",
                    value="kontakt@example.com",
                    source_type="test-enrichment",
                    is_verified=True,
                    confidence_score=90,
                )
            )
            selected.contacts_checked_at = selected.updated_at
            db.session.commit()
            return {"processed_companies": 1, "errors": []}

        enrich.side_effect = add_email

        result = run_campaign_automation(campaign)

        self.assertEqual(result["delivery"]["sent"], 1)
        enrich.assert_called_once()
        self.assertEqual(Lead.query.one().email, "kontakt@example.com")

    def test_automation_runs_only_once_per_day_and_pause_stops_it(self):
        campaign = Campaign(
            name="Bez kandidátov",
            offer_type="product",
            offer_stage="validation",
            offer_description="Test produktu",
            subject_template="Predmet",
            body_template="Pripravujeme test produktu.",
            status="active",
            automation_enabled=True,
            targeting_profile={
                "ideal_customer_profile": "Servisy",
                "nace_keywords": ["4520"],
                "company_keywords": [],
                "minimum_fit_score": 60,
            },
            target_total=50,
            batch_size=10,
            daily_limit=10,
            follow_up_days=7,
            contact_cooldown_days=90,
        )
        db.session.add(campaign)
        db.session.commit()

        first = run_campaign_automation(campaign)
        second = run_campaign_automation(campaign)

        self.assertEqual(first["delivery"]["sent"], 0)
        self.assertIn("Dnešná dávka", second["skipped"])

        campaign.status = "paused"
        campaign.last_automation_run_at = None
        db.session.commit()
        paused = run_campaign_automation(campaign)
        self.assertIn("nie je aktívna", paused["skipped"])

    def test_geonames_parser_normalizes_postal_code(self):
        rows = parse_geonames_postal_text(
            "SK\t058 01\tPoprad\tPrešovský kraj\tPV\tPoprad\t706\t\t\t49.055\t20.305\t4"
        )

        self.assertEqual(rows[0]["postal_code"], "05801")
        self.assertEqual(rows[0]["search_name"], "poprad")


if __name__ == "__main__":
    unittest.main()
