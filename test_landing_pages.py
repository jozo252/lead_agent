import unittest
from unittest.mock import patch

from app import create_app
from extensions import db
from models import Campaign, LandingPage
from services.landing_pages import (
    available_slug,
    default_landing_page_content,
    normalize_landing_page_content,
)


class LandingPageTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "SQLALCHEMY_DATABASE_URI": "sqlite://",
                "WTF_CSRF_ENABLED": False,
                "MAIL_DEFAULT_SENDER": "Adam <adam@example.test>",
                "PUBLIC_BASE_URL": "https://offers.example.test",
                "LANDING_OPERATOR_NAME": "Gallax, s. r. o.",
                "LANDING_OPERATOR_ADDRESS": "Testovacia 1, Bratislava",
                "LANDING_OPERATOR_ICO": "12345678",
                "LANDING_OPERATOR_PHONE": "+421 900 000 000",
                "LANDING_OPERATOR_REGISTER": "OR MS Bratislava III, oddiel Sro",
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

    def add_campaign(self, name="AI recepčný", stage="validation"):
        campaign = Campaign(
            name=name,
            offer_type="product",
            offer_stage=stage,
            offer_description="AI recepčný pre zmeškané telefonáty v autoservise.",
            subject_template="Otázka",
            body_template="Pripravujeme riešenie.",
            targeting_profile={
                "ideal_customer_profile": "Menšie autoservisy s telefonickými objednávkami."
            },
        )
        db.session.add(campaign)
        db.session.commit()
        return campaign

    def form_data(self, campaign, **overrides):
        content = default_landing_page_content(campaign)
        data = {
            "slug": "ai-recepcny",
            "brand_name": content["brand_name"],
            "eyebrow": content["eyebrow"],
            "headline": content["headline"],
            "subheadline": content["subheadline"],
            "problem_title": content["problem_title"],
            "problem_text": content["problem_text"],
            "solution_title": content["solution_title"],
            "solution_text": content["solution_text"],
            "process_title": content["process_title"],
            "audience_title": content["audience_title"],
            "audience_text": content["audience_text"],
            "cta_title": content["cta_title"],
            "cta_text": content["cta_text"],
            "cta_label": content["cta_label"],
            "meta_description": content["meta_description"],
            "contact_email": "adam@example.test",
            "hero_image_url": "",
            "hero_image_alt": "",
        }
        for index, item in enumerate(content["benefits"], start=1):
            data[f"benefit_title_{index}"] = item["title"]
            data[f"benefit_text_{index}"] = item["text"]
        for index, item in enumerate(content["steps"], start=1):
            data[f"step_title_{index}"] = item["title"]
            data[f"step_text_{index}"] = item["text"]
        data.update(overrides)
        return data

    @patch("landing_page_routes.generate_landing_page_copy")
    def test_create_builds_private_draft_and_token_preview(self, generate_copy):
        campaign = self.add_campaign()
        generated = default_landing_page_content(campaign)
        generated["headline"] = "Žiadny zmeškaný hovor bez odpovede"
        generate_copy.return_value = generated

        response = self.client.post(
            f"/campaigns/{campaign.id}/landing-page/create"
        )

        self.assertEqual(response.status_code, 302)
        landing_page = LandingPage.query.one()
        self.assertEqual(landing_page.status, "draft")
        self.assertEqual(landing_page.contact_email, "adam@example.test")
        self.assertEqual(
            landing_page.content["headline"],
            "Žiadny zmeškaný hovor bez odpovede",
        )
        self.assertEqual(
            self.client.get(f"/ponuka/{landing_page.slug}").status_code,
            404,
        )

        preview = self.client.get(
            f"/ponuka/{landing_page.slug}?preview={landing_page.preview_token}"
        )
        self.assertEqual(preview.status_code, 200)
        self.assertIn("no-store", preview.headers["Cache-Control"])
        self.assertIn("noindex", preview.headers["X-Robots-Tag"])
        self.assertIn("Žiadny zmeškaný hovor", preview.get_data(as_text=True))
        self.assertEqual(
            self.client.get(
                f"/campaigns/{campaign.id}/landing-page"
            ).status_code,
            200,
        )

    def test_publish_exposes_page_and_enforces_validation_disclosure(self):
        campaign = self.add_campaign()
        landing_page = LandingPage(
            campaign=campaign,
            slug="povodny-slug",
            preview_token="secret-preview-token",
            status="draft",
            content=normalize_landing_page_content({}, campaign),
        )
        db.session.add(landing_page)
        db.session.commit()

        response = self.client.post(
            f"/campaigns/{campaign.id}/landing-page/update",
            data=self.form_data(
                campaign,
                action="publish",
                headline="AI <script>alert(1)</script> recepčný",
                hero_image_url="https://images.example.test/reception.jpg",
                hero_image_alt="Ukážka telefonického asistenta",
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(landing_page.status, "published")
        self.assertIsNotNone(landing_page.published_at)
        public = self.client.get("/ponuka/ai-recepcny")
        html = public.get_data(as_text=True)
        self.assertEqual(public.status_code, 200)
        self.assertIn("nejde ešte o hotový produkt", html)
        self.assertIn("AI &lt;script&gt;alert(1)&lt;/script&gt; recepčný", html)
        self.assertNotIn("LeadFlow", html)
        self.assertIn("https://images.example.test/reception.jpg", html)
        self.assertIn("Gallax, s. r. o.", html)
        self.assertIn("12345678", html)
        self.assertIn(
            "frame-ancestors 'none'",
            public.headers["Content-Security-Policy"],
        )
        self.assertEqual(public.headers["X-Frame-Options"], "DENY")

    def test_publish_fails_closed_without_operator_identity(self):
        self.app.config.update(
            LANDING_OPERATOR_NAME="",
            LANDING_OPERATOR_ADDRESS="",
            LANDING_OPERATOR_ICO="",
            LANDING_OPERATOR_PHONE="",
            LANDING_OPERATOR_REGISTER="",
        )
        campaign = self.add_campaign(stage="ready")
        landing_page = LandingPage(
            campaign=campaign,
            slug="bez-prevadzkovatela",
            preview_token="operator-preview-token",
            status="draft",
            content=normalize_landing_page_content({}, campaign),
        )
        db.session.add(landing_page)
        db.session.commit()

        response = self.client.post(
            f"/campaigns/{campaign.id}/landing-page/update",
            data=self.form_data(campaign, action="publish"),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(landing_page.status, "draft")

    def test_invalid_image_cannot_be_published(self):
        campaign = self.add_campaign(stage="ready")
        landing_page = LandingPage(
            campaign=campaign,
            slug="bezpecna-ponuka",
            preview_token="another-preview-token",
            status="draft",
            content=normalize_landing_page_content({}, campaign),
        )
        db.session.add(landing_page)
        db.session.commit()

        response = self.client.post(
            f"/campaigns/{campaign.id}/landing-page/update",
            data=self.form_data(
                campaign,
                action="publish",
                hero_image_url="http://insecure.example.test/image.jpg",
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(landing_page.status, "draft")
        self.assertIsNone(landing_page.hero_image_url)

    def test_local_static_image_is_rejected_when_only_offer_route_is_public(self):
        campaign = self.add_campaign(stage="ready")
        landing_page = LandingPage(
            campaign=campaign,
            slug="lokalny-obrazok",
            preview_token="local-image-preview-token",
            status="draft",
            content=normalize_landing_page_content({}, campaign),
        )
        db.session.add(landing_page)
        db.session.commit()

        response = self.client.post(
            f"/campaigns/{campaign.id}/landing-page/update",
            data=self.form_data(
                campaign,
                action="save",
                hero_image_url="/static/private-looking-image.jpg",
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(landing_page.hero_image_url)

    def test_slug_collision_gets_stable_suffix(self):
        first_campaign = self.add_campaign(name="AI Recepčný")
        second_campaign = self.add_campaign(name="AI Recepčný 2")
        db.session.add(
            LandingPage(
                campaign=first_campaign,
                slug="ai-recepcny",
                preview_token="first-preview-token",
                status="draft",
                content=normalize_landing_page_content({}, first_campaign),
            )
        )
        db.session.commit()

        self.assertEqual(available_slug("AI Recepčný"), "ai-recepcny-2")
        self.assertEqual(second_campaign.landing_page, None)


if __name__ == "__main__":
    unittest.main()
