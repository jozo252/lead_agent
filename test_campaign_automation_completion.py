import unittest
from datetime import datetime, timezone

from app import create_app
from extensions import db
from models import Campaign, CampaignFollowUp, CampaignRecipient, Company, Lead, OutboundEmail, SenderProfile
from services.campaign_automation import run_campaign_automation


class CampaignCompletionTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test", "SQLALCHEMY_DATABASE_URI": "sqlite://", "WTF_CSRF_ENABLED": False, "MAIL_SUPPRESS_SEND": True})
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.campaign = Campaign(name="Test", offer_description="Test", subject_template="Test", body_template="Test", automation_enabled=True, status="active", target_total=1, follow_up_enabled=True)
        company = Company(official_name="Test", ico="12345678")
        profile = SenderProfile(name="Weby", config_key="WEBS")
        recipient = CampaignRecipient(campaign=self.campaign, company=company, recipient_email="hello@example.test", subject="Test", body="Test", status="sent", sent_at=datetime.now(timezone.utc))
        lead = Lead(company=company, company_name="Test", email="hello@example.test")
        outbound = OutboundEmail(lead=lead, campaign_recipient=recipient, sender_profile=profile, recipient="hello@example.test", subject="Test", body="Test", message_id="<original@example.test>")
        self.followup = CampaignFollowUp(campaign_recipient=recipient, original_outbound=outbound, sender_profile=profile, subject="Follow-up", body="Test", due_at=datetime.now(timezone.utc), status="scheduled")
        db.session.add(self.followup)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_pending_and_uncertain_followups_keep_campaign_active_at_target(self):
        for status in ["scheduled", "sending", "unknown"]:
            with self.subTest(status=status):
                self.followup.status = status
                db.session.commit()
                result = run_campaign_automation(self.campaign, force=True)
                self.assertIn("skipped", result)
                self.assertEqual(self.campaign.status, "active")
                self.assertIsNone(self.campaign.completed_at)

    def test_terminal_followups_allow_campaign_completion(self):
        for status in ["sent", "cancelled", "suppressed"]:
            with self.subTest(status=status):
                self.campaign.status = "active"
                self.campaign.completed_at = None
                self.followup.status = status
                db.session.commit()
                run_campaign_automation(self.campaign, force=True)
                self.assertEqual(self.campaign.status, "completed")
                self.assertIsNotNone(self.campaign.completed_at)

    def test_followups_disabled_does_not_hold_campaign_open(self):
        self.campaign.follow_up_enabled = False
        db.session.commit()
        run_campaign_automation(self.campaign, force=True)
        self.assertEqual(self.campaign.status, "completed")


if __name__ == "__main__":
    unittest.main()
