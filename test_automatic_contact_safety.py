import unittest

from app import create_app
from extensions import db
from models import Campaign, CampaignRecipient, Company, CompanyContact


class AutomaticContactSafetyTests(unittest.TestCase):
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

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _recipient(self, *, verified, email, ico, recipient_email=None):
        company = Company(official_name=f"Firma {ico}", ico=ico)
        contact = CompanyContact(
            company=company,
            contact_type="email",
            value=email,
            source_type="test",
            is_verified=verified,
            is_primary=True,
            confidence_score=100,
        )
        db.session.add(company)
        db.session.flush()
        recipient = CampaignRecipient(
            campaign=self.campaign,
            company=company,
            contact=contact,
            recipient_email=recipient_email or email,
            subject="Test",
            body="Test",
            selection_source="automation",
            status="draft",
        )
        db.session.add(recipient)
        return recipient

    def test_activation_only_auto_approves_verified_contacts(self):
        self.campaign = Campaign(
            name="Bezpečná automatizácia",
            offer_type="service",
            offer_description="Test",
            subject_template="Test",
            body_template="Test",
            automation_enabled=True,
            targeting_profile={"nace_keywords": ["43.21"]},
        )
        db.session.add(self.campaign)
        db.session.flush()
        verified = self._recipient(
            verified=True,
            email="verified@example.com",
            ico="10000001",
        )
        unverified = self._recipient(
            verified=False,
            email="unverified@example.com",
            ico="10000002",
        )
        mismatched = self._recipient(
            verified=True,
            email="verified-two@example.com",
            recipient_email="other@example.com",
            ico="10000003",
        )
        db.session.commit()

        response = self.client.post(
            f"/campaigns/{self.campaign.id}/status",
            data={"status": "active"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(verified.status, "approved")
        self.assertIsNotNone(verified.approved_at)
        self.assertEqual(unverified.status, "draft")
        self.assertIn("overený e-mail", unverified.last_error)
        self.assertEqual(mismatched.status, "draft")
        self.assertIn("overený e-mail", mismatched.last_error)

    def test_manual_approval_of_automatic_recipient_requires_verified_match(self):
        self.campaign = Campaign(
            name="Bezpečná automatizácia",
            offer_type="service",
            offer_description="Test",
            subject_template="Test",
            body_template="Test",
            automation_enabled=True,
            targeting_profile={"nace_keywords": ["43.21"]},
        )
        db.session.add(self.campaign)
        db.session.flush()
        recipient = self._recipient(
            verified=False,
            email="unverified@example.com",
            ico="10000004",
        )
        db.session.commit()

        response = self.client.post(
            f"/campaigns/{self.campaign.id}/recipients/{recipient.id}",
            data={"action": "approve", "subject": "Test", "body": "Test"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(recipient.status, "draft")
        self.assertIsNone(recipient.approved_at)
        self.assertIn("overený", recipient.last_error)


if __name__ == "__main__":
    unittest.main()
