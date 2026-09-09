"""Inbox-only CLI checks with isolated data and blocked network transports."""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import create_app
from commands.followups import sync_campaign_inbox_command
from extensions import db
from models import Campaign, CampaignRecipient, Company, EmailReply, Lead, OutboundEmail, SenderProfile, Suppression


class CampaignInboxCommandTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.dotenv = patch("app.load_dotenv", return_value=False)
        self.dotenv.start()
        self.addCleanup(self.dotenv.stop)
        for target in ("socket.socket.connect", "socket.socket.connect_ex"):
            guard = patch(target, side_effect=AssertionError("Unexpected network call"))
            guard.start()
            self.addCleanup(guard.stop)
        self.send = patch("services.campaign_followups.send_profile_message")
        self.send_mock = self.send.start()
        self.addCleanup(self.send.stop)
        self.app = create_app({
            "TESTING": True, "SECRET_KEY": "test-only", "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite://", "MAIL_SUPPRESS_SEND": True,
            "SENDER_PROFILE_SETTINGS": {
                "WEBS": self.settings("webs@example.test"),
                "OTHER": self.settings("other@example.test"),
            },
        })
        self.app.cli.add_command(sync_campaign_inbox_command)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.now = datetime.now(timezone.utc).replace(tzinfo=None)
        self.profile = SenderProfile(name="Weby", config_key="WEBS", sender_name="Test",
                                     sender_email="webs@example.test", enabled=True)
        self.other_profile = SenderProfile(name="Other", config_key="OTHER", sender_name="Test",
                                           sender_email="other@example.test", enabled=True)
        self.campaign = Campaign(name="Salóny", offer_description="Test", subject_template="Test",
                                 body_template="Test", status="paused", sender_profile=self.profile)
        self.other_campaign = Campaign(name="Other", offer_description="Test", subject_template="Test",
                                       body_template="Test", status="active", sender_profile=self.other_profile)
        db.session.add_all([self.campaign, self.other_campaign])
        db.session.commit()

    @staticmethod
    def settings(address):
        return {"MAIL_SERVER": "smtp.example.test", "MAIL_PORT": 587, "MAIL_USE_TLS": True,
                "MAIL_USERNAME": address, "MAIL_PASSWORD": "test-only",
                "IMAP_SERVER": "imap.example.test", "IMAP_PORT": 993,
                "IMAP_USERNAME": address, "IMAP_PASSWORD": "test-only"}

    def tearDown(self):
        self.send_mock.assert_not_called()
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def invoke(self, campaign_id=None):
        result = self.app.test_cli_runner().invoke(args=[
            "sync-campaign-inbox", "--campaign-id", str(campaign_id or self.campaign.id),
        ])
        return result, json.loads(result.output)

    def add_outbound(self, campaign, index, days):
        profile = campaign.sender_profile
        company = Company(official_name=f"Salon {index}", ico=f"1000000{index}")
        address = f"salon{index}@example.test"
        lead = Lead(company=company, company_name=company.official_name, email=address, status="Oslovený")
        recipient = CampaignRecipient(campaign=campaign, company=company, recipient_email=address,
                                      subject="Test", body="Test", status="sent")
        outbound = OutboundEmail(lead=lead, campaign_recipient=recipient, sender_profile=profile,
                                 recipient=address, subject="Test", body="Test", message_id=f"<initial{index}@example.test>",
                                 sent_at=self.now - timedelta(days=days))
        db.session.add(outbound)
        db.session.commit()
        return outbound

    @patch("services.campaign_followups.fetch_profile_messages", return_value=[])
    def test_paused_campaign_syncs_without_due_followups_or_send(self, fetch):
        result, payload = self.invoke()
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(payload["synced"])
        self.assertEqual(payload["sent"], 0)
        self.assertEqual(self.campaign.status, "paused")
        self.assertFalse(self.campaign.automation_enabled)
        self.assertIsNotNone(self.profile.last_synced_at)
        fetch.assert_called_once_with(self.profile, since_datetime=None)

    @patch("services.campaign_followups.fetch_profile_messages")
    def test_full_account_window_imports_late_optout_and_ignores_other_profile(self, fetch):
        oldest = self.add_outbound(self.campaign, 1, 30)
        self.add_outbound(self.campaign, 2, 4)
        other = self.add_outbound(self.other_campaign, 3, 50)
        self.profile.last_synced_at = self.now - timedelta(days=1)
        db.session.commit()
        fetch.return_value = [
            {"message_id": "<late-optout@example.test>", "from_email": oldest.recipient,
             "subject": "Re: Test", "body": "neposielať", "received_at": self.now,
             "thread_message_ids": [oldest.message_id]},
            {"message_id": "<other-reply@example.test>", "from_email": other.recipient,
             "subject": "Re: Test", "body": "Odpoveď", "received_at": self.now,
             "thread_message_ids": [other.message_id]},
        ]
        result, payload = self.invoke()
        self.assertEqual(result.exit_code, 0)
        fetch.assert_called_once_with(self.profile, since_datetime=oldest.sent_at)
        self.assertEqual(payload["inbox"]["imported"], 1)
        self.assertEqual(payload["inbox"]["skipped"], 1)
        self.assertEqual(EmailReply.query.one().sender_profile_id, self.profile.id)
        self.assertEqual(Suppression.query.one().value, oldest.recipient)
        self.assertEqual(oldest.campaign_recipient.status, "opted_out")
        self.assertEqual(other.campaign_recipient.status, "sent")
        self.assertIsNone(self.other_profile.last_synced_at)
        self.assertEqual(OutboundEmail.query.count(), 3)

    @patch("services.campaign_followups.fetch_profile_messages")
    def test_missing_campaign_or_profile_fail_before_inbox(self, fetch):
        result, payload = self.invoke(9999)
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(payload["synced"])
        self.campaign.sender_profile = None
        db.session.commit()
        result, payload = self.invoke()
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(payload["synced"])
        fetch.assert_not_called()

    @patch("services.campaign_followups.fetch_profile_messages")
    def test_disabled_profile_or_missing_credentials_fail_before_inbox(self, fetch):
        self.profile.enabled = False
        db.session.commit()
        result, payload = self.invoke()
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(payload["synced"])
        self.profile.enabled = True
        db.session.commit()
        self.app.config["SENDER_PROFILE_SETTINGS"]["WEBS"].pop("IMAP_PASSWORD")
        result, payload = self.invoke()
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(payload["synced"])
        fetch.assert_not_called()

    @patch("services.campaign_followups.fetch_profile_messages", side_effect=RuntimeError("PASSWORD=private-marker"))
    def test_incomplete_scan_fails_nonzero_without_secret_or_success_timestamp(self, fetch):
        result, payload = self.invoke()
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(payload["synced"])
        self.assertEqual(payload["sent"], 0)
        self.assertNotIn("private-marker", result.output)
        self.assertIsNone(self.profile.last_synced_at)
        self.assertIsNotNone(self.profile.last_sync_error)

    def test_campaign_id_is_required_and_positive(self):
        for args in ([], ["--campaign-id", "0"], ["--campaign-id", "-1"]):
            with self.subTest(args=args):
                result = self.app.test_cli_runner().invoke(args=["sync-campaign-inbox", *args])
                self.assertEqual(result.exit_code, 2)


if __name__ == "__main__":
    unittest.main()
