"""Isolated checks for opt-in privacy copy and recurring salon selection."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import create_app
from extensions import db
from models import Campaign, CampaignRecipient, Company, CompanyContact
from services.campaign_automation import campaign_candidates, prepare_automated_recipients
from services.campaigns import OPT_OUT_FOOTER, ensure_campaign_privacy_disclosure, render_campaign_template


class CampaignPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True, "SECRET_KEY": "test-only", "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "MAIL_SUPPRESS_SEND": True,
        })
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        for target in ("requests.sessions.Session.request", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("Unexpected external network"))
            guard.start()
            self.addCleanup(guard.stop)
        self.campaign = Campaign(
            name="Salon test", offer_description="Ukážka webu", subject_template="Web pre {company_name}",
            body_template="Dobrý deň,\n\nPripravil som ukážku.\n\n" + OPT_OUT_FOOTER,
            automation_enabled=True, status="active", target_total=20,
            targeting_profile={
                "nace_keywords": ["9602"], "copy_mode": "fixed_template",
                "privacy_notice_url": "https://gallax.io/informacie-o-spracuvani-osobnych-udajov",
            },
        )
        db.session.add(self.campaign)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def contact(self, name="Salon", *, verified=True, source="https://example.com/salon/kontakt", source_type="test", age=0):
        number = Company.query.count() + 1
        company = Company(
            official_name=name, ico=str(10000000 + number), sk_nace_code="9602",
            contacts_checked_at=datetime.now(timezone.utc),
        )
        contact = CompanyContact(
            company=company, contact_type="email", value=f"salon{number}@example.com",
            is_verified=verified, source_url=source, source_type=source_type,
            last_verified_at=datetime.now(timezone.utc) - timedelta(days=age),
        )
        db.session.add(contact)
        db.session.commit()
        return contact

    @staticmethod
    def rank(campaign, candidates):
        return [{"company_id": company.id, "fit_score": 80, "fit_reason": "NACE", "subject": "AI subject", "body": "AI replacement copy"} for company in candidates]

    def prepare(self, requested=1):
        with patch("services.campaign_automation.rank_campaign_candidates", side_effect=self.rank):
            return prepare_automated_recipients(self.campaign, requested)

    def test_legacy_campaign_copy_and_template_calls_are_unchanged(self):
        contact = self.contact(source=None)
        self.campaign.targeting_profile = {"nace_keywords": ["9602"]}
        body = "Original {email_source_url}"
        self.assertEqual(ensure_campaign_privacy_disclosure(body, self.campaign, contact), body)
        self.assertEqual(render_campaign_template(body, contact.company), body)
        self.assertEqual(render_campaign_template(body, contact.company, email_source_url="https://example.com/"), "Original https://example.com/")

    def test_disclosure_uses_exact_source_and_is_idempotent_before_footer(self):
        contact = self.contact(source="https://example.com/salon/contact?ref=public#email")
        body = ensure_campaign_privacy_disclosure("Ponuka\n\n" + OPT_OUT_FOOTER, self.campaign, contact)
        self.assertIn("Zdroj kontaktu: https://example.com/salon/contact?ref=public.", body)
        self.assertNotIn("#email", body)
        self.assertIn("na základe oprávneného záujmu", body)
        self.assertTrue(body.endswith(OPT_OUT_FOOTER))
        self.assertEqual(ensure_campaign_privacy_disclosure(body, self.campaign, contact), body)

    def test_missing_or_nonpublic_sources_are_rejected(self):
        contact = self.contact()
        for source in (None, "", "{email_source_url}", "http://127.0.0.1/contact", "https://192.168.1.2/contact", "http://127.1/contact", "http://2130706433/contact", "http://localhost/", "https://crm.internal/contact", "https://bad_host.example/contact", "https://user:password@example.com/contact", "javascript:alert(1)", "https://example.com/a b"):
            with self.subTest(source=source):
                contact.source_url = source
                with self.assertRaises(ValueError):
                    ensure_campaign_privacy_disclosure("Ponuka", self.campaign, contact)

    def test_empty_notice_and_unresolved_copy_fail_closed(self):
        contact = self.contact()
        for body in ("Ponuka pre {company_name}", "<DOPLNIŤ IDENTITU>"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                ensure_campaign_privacy_disclosure(body, self.campaign, contact)
        self.campaign.targeting_profile = {"privacy_notice_url": ""}
        with self.assertRaises(ValueError):
            ensure_campaign_privacy_disclosure("Ponuka", self.campaign, contact)

    def test_fixed_copy_preserves_approved_greeting_and_ignores_ai_copy(self):
        contact = self.contact()
        result = self.prepare()
        self.assertEqual(result["prepared"], 1)
        recipient = CampaignRecipient.query.one()
        self.assertEqual(recipient.subject, "Web pre Salon")
        self.assertTrue(recipient.body.startswith("Dobrý deň,\n\nPripravil som ukážku."))
        self.assertNotIn("AI replacement", recipient.body)
        self.assertIn("Zdroj kontaktu: " + contact.source_url, recipient.body)
        self.assertTrue(recipient.body.endswith(OPT_OUT_FOOTER))
        self.assertEqual(recipient.status, "approved")

    def test_bad_source_skips_only_that_contact_and_next_valid_one_is_prepared(self):
        self.contact("A invalid", source=None)
        valid = self.contact("B valid")
        result = self.prepare()
        self.assertEqual(result["prepared"], 1)
        self.assertEqual(result["without_email"], 1)
        self.assertEqual(CampaignRecipient.query.one().contact_id, valid.id)

    def test_ai_copy_cannot_omit_opted_in_disclosure(self):
        self.contact()
        self.campaign.targeting_profile = {**self.campaign.targeting_profile, "copy_mode": "ai"}
        self.prepare()
        recipient = CampaignRecipient.query.one()
        self.assertTrue(recipient.body.startswith("AI replacement copy"))
        self.assertIn("Zdroj kontaktu:", recipient.body)
        self.assertIn("Podrobnosti: https://gallax.io/", recipient.body)

    def test_checked_unverified_pool_cannot_starve_verified_contact(self):
        for number in range(35):
            self.contact(f"A Salon {number:02}", verified=False)
        valid = self.contact("Z valid salon")
        self.assertEqual(len(campaign_candidates(self.campaign, 30)), 30)
        result = self.prepare()
        self.assertEqual(result["prepared"], 1)
        self.assertEqual(CampaignRecipient.query.one().contact_id, valid.id)

    def test_salon_mode_requires_fresh_observed_listing_contact(self):
        self.campaign.targeting_profile = {**self.campaign.targeting_profile, "salon_discovery": True}
        self.contact("A Other source")
        self.contact("B Old source", source_type="salon_public_listing", age=8)
        self.contact("C Future date", source_type="salon_public_listing", age=-1)
        self.contact("D Unverified", source_type="salon_public_listing", verified=False)
        valid = self.contact("Z current listing", source_type="salon_public_listing", age=2)
        alternate = CompanyContact(company=valid.company, contact_type="email", value="other@example.com", source_type="test", is_verified=True, is_primary=True, confidence_score=100)
        db.session.add(alternate)
        db.session.commit()
        candidates = campaign_candidates(self.campaign, 30, require_verified=True)
        self.assertEqual([company.id for company in candidates], [valid.company_id])
        self.assertEqual(self.prepare()["prepared"], 1)
        self.assertEqual(CampaignRecipient.query.one().contact_id, valid.id)


if __name__ == "__main__":
    unittest.main()
