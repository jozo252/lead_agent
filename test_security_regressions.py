import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app import create_app
from extensions import db
from models import (
    Campaign,
    CampaignFollowUp,
    CampaignRecipient,
    Company,
    CompanyContact,
    EmailReply,
    Lead,
    OutboundEmail,
    SenderProfile,
    Suppression,
)
from services.campaign_followups import sync_profile_inbox
from services.campaigns import is_explicit_opt_out_reply


class FollowUpSecurityRegressionTests(unittest.TestCase):
    def setUp(self):
        settings = {
            "MAIL_SERVER": "smtp.example.test",
            "MAIL_PORT": 587,
            "MAIL_USE_TLS": True,
            "MAIL_USE_SSL": False,
            "MAIL_USERNAME": "webs@example.test",
            "MAIL_PASSWORD": "test-only",
            "IMAP_SERVER": "imap.example.test",
            "IMAP_PORT": 993,
            "IMAP_USERNAME": "webs@example.test",
            "IMAP_PASSWORD": "test-only",
        }
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-only",
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "WTF_CSRF_ENABLED": False,
            "MAIL_SUPPRESS_SEND": True,
            "MAIL_DEFAULT_SENDER": "legacy@example.test",
            "SENDER_PROFILE_SETTINGS": {"WEBS": settings},
        })
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()
        self.now = datetime(2026, 9, 7, 9, 0)

        self.profile = SenderProfile(
            name="Weby",
            config_key="WEBS",
            sender_name="Web tím",
            sender_email="webs@example.test",
            signature="Web tím",
            enabled=True,
        )
        self.company = Company(official_name="Bezpečný salón", ico="90909090")
        self.contact = CompanyContact(
            company=self.company,
            contact_type="email",
            value="salon@example.test",
            source_type="test",
            is_verified=True,
        )
        self.campaign = Campaign(
            name="Weby",
            offer_description="Web",
            subject_template="Web",
            body_template="Ponuka",
            sender_profile=self.profile,
            status="active",
            daily_limit=3,
            follow_up_enabled=True,
            follow_up_days=3,
            follow_up_approved_at=self.now,
            follow_up_subject_template="Re: Web",
            follow_up_body_template="Pripomínam ponuku.",
        )
        self.lead = Lead(
            company=self.company,
            company_name=self.company.official_name,
            email=self.contact.value,
            status="Oslovený",
            last_contacted_at=self.now - timedelta(days=4),
        )
        self.recipient = CampaignRecipient(
            campaign=self.campaign,
            company=self.company,
            contact=self.contact,
            recipient_email=self.contact.value,
            subject="Web",
            body="Ponuka",
            status="sent",
            sent_at=self.now - timedelta(days=4),
        )
        self.outbound = OutboundEmail(
            lead=self.lead,
            campaign_recipient=self.recipient,
            sender_profile=self.profile,
            message_id="<security-original@example.test>",
            recipient=self.contact.value,
            subject="Web",
            body="Ponuka",
            sent_at=self.now - timedelta(days=4),
        )
        self.followup = CampaignFollowUp(
            campaign_recipient=self.recipient,
            original_outbound=self.outbound,
            sender_profile=self.profile,
            due_at=self.now - timedelta(days=1),
            subject="Re: Web",
            body="Pripomínam ponuku.",
            status="scheduled",
        )
        db.session.add_all([self.contact, self.outbound, self.followup])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def incoming(self, body="Mám záujem.", message_id="<security-reply@example.test>"):
        return {
            "from_email": self.contact.value,
            "from_name": self.company.official_name,
            "subject": "Re: Web",
            "body": body,
            "message_id": message_id,
            "received_at": self.now - timedelta(days=1),
            "thread_message_ids": [self.outbound.message_id],
        }

    def test_slovak_gmail_quote_is_removed_before_opt_out_detection(self):
        gmail_reply = (
            "Neposielať\n\n"
            "V so 6. 9. 2026 o 18:42 používateľ Adam <adam@gallax.io> "
            "napísal(a):\n"
            "> Ak si neželáte ďalšie správy, stačí odpovedať „neposielať“."
        )
        positive_reply = gmail_reply.replace("Neposielať\n\n", "Mám záujem.\n\n", 1)
        self.assertTrue(is_explicit_opt_out_reply(gmail_reply))
        self.assertFalse(is_explicit_opt_out_reply(positive_reply))

    @patch("routes.check_reply_from_sender")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_lead_check_uses_campaign_profile_and_never_global_mailbox(
        self, fetch_profile, legacy_check
    ):
        fetch_profile.return_value = [self.incoming()]

        response = self.client.post(f"/lead/{self.lead.id}/check-reply")

        self.assertEqual(response.status_code, 302)
        legacy_check.assert_not_called()
        fetch_profile.assert_called_once()
        self.assertEqual(fetch_profile.call_args.args[0].id, self.profile.id)
        reply = EmailReply.query.one()
        self.assertEqual(reply.sender_profile_id, self.profile.id)
        self.assertEqual(reply.campaign_recipient_id, self.recipient.id)
        self.assertEqual(self.followup.status, "cancelled")

    @patch("services.campaign_followups.fetch_profile_messages")
    def test_profile_sync_reconciles_old_unprofiled_copy_of_same_message(self, fetch):
        existing = EmailReply(
            lead=self.lead,
            campaign_recipient=self.recipient,
            from_email=self.contact.value,
            subject="Re: Web",
            text_body="Mám záujem.",
            imap_message_id="<security-reply@example.test>",
            received_at=self.now - timedelta(days=1),
        )
        db.session.add(existing)
        db.session.commit()
        fetch.return_value = [self.incoming()]

        result = sync_profile_inbox(self.profile)

        self.assertEqual(result["reconciled"], 1)
        self.assertEqual(EmailReply.query.count(), 1)
        self.assertEqual(existing.sender_profile_id, self.profile.id)
        self.assertEqual(self.followup.status, "cancelled")

    @patch("services.campaign_followups.fetch_profile_messages")
    def test_gmail_opt_out_persists_suppression_and_is_idempotent(self, fetch):
        body = (
            "Neposielať\n\n"
            "Dňa 6. 9. 2026 používateľ Adam <adam@gallax.io> napísal(a):\n"
            "> Pôvodná ponuka"
        )
        fetch.return_value = [self.incoming(body=body)]

        first = sync_profile_inbox(self.profile)
        second = sync_profile_inbox(self.profile)

        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(EmailReply.query.count(), 1)
        self.assertEqual(Suppression.query.count(), 1)
        self.assertEqual(Suppression.query.one().value, self.contact.value)
        self.assertEqual(self.recipient.status, "opted_out")
        self.assertEqual(self.followup.status, "cancelled")

    def test_main_inbox_lists_sender_profiles(self):
        page = self.client.get("/inbox")
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="sender_profile_id"', html)
        self.assertIn("Weby · webs@example.test", html)

    def test_external_referrer_cannot_control_post_redirect(self):
        response = self.client.post(
            f"/lead/{self.lead.id}/save-message",
            data={"suggested_message": "Bezpečný návrh", "email_subject": "Ponuka"},
            headers={"Referer": "https://attacker.example/steal"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

    def test_same_origin_referrer_is_reduced_to_a_safe_local_path(self):
        response = self.client.post(
            f"/lead/{self.lead.id}/save-message",
            data={"suggested_message": "Bezpečný návrh", "email_subject": "Ponuka"},
            headers={"Referer": "http://localhost//attacker.example/path?tab=1"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/attacker.example/path?tab=1")

    @patch("routes.mail.send", side_effect=RuntimeError("provider-secret-marker"))
    def test_legacy_send_error_does_not_leak_provider_details(self, send):
        legacy_lead = Lead(
            company_name="Legacy firma",
            email="legacy-recipient@example.test",
            status="Osloviť",
        )
        db.session.add(legacy_lead)
        db.session.commit()

        response = self.client.post(
            f"/lead/{legacy_lead.id}/send-email",
            data={"email_subject": "Ponuka", "suggested_message": "Text"},
            follow_redirects=True,
        )

        send.assert_called_once()
        self.assertNotIn("provider-secret-marker", response.get_data(as_text=True))
        self.assertEqual(OutboundEmail.query.filter_by(lead_id=legacy_lead.id).count(), 0)

    @patch("routes.mail.send")
    def test_legacy_send_rejects_subject_header_injection(self, send):
        legacy_lead = Lead(
            company_name="Legacy firma",
            email="legacy-recipient@example.test",
            status="Osloviť",
        )
        db.session.add(legacy_lead)
        db.session.commit()

        response = self.client.post(
            f"/lead/{legacy_lead.id}/send-email",
            data={
                "email_subject": "Ponuka\r\nBcc: hidden@example.test",
                "suggested_message": "Text",
            },
            follow_redirects=True,
        )

        send.assert_not_called()
        self.assertIn("jeden platný riadok", response.get_data(as_text=True))

    @patch("services.manual_replies.send_profile_message")
    def test_repeated_manual_reply_post_sends_exactly_once(self, send):
        reply = EmailReply(
            lead=self.lead,
            campaign_recipient=self.recipient,
            sender_profile=self.profile,
            from_email=self.contact.value,
            subject="Re: Web",
            text_body="Prosím o viac informácií.",
            imap_message_id="<manual-inbound@example.test>",
            received_at=self.now,
        )
        db.session.add(reply)
        db.session.commit()

        def assert_claimed(message, profile):
            stored = db.session.get(EmailReply, reply.id)
            self.assertEqual(stored.reply_delivery_status, "sending")
            self.assertEqual(message.msgId, stored.reply_message_id)
            self.assertEqual(profile.id, self.profile.id)

        send.side_effect = assert_claimed
        first = self.client.post(
            f"/reply/{reply.id}/send", data={"reply_body": "Ďakujem."}
        )
        second = self.client.post(
            f"/reply/{reply.id}/send", data={"reply_body": "Ďakujem."}
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        send.assert_called_once()
        self.assertEqual(reply.reply_delivery_status, "sent")
        self.assertIsNotNone(reply.reply_sent_at)
        self.assertEqual(OutboundEmail.query.count(), 2)
        self.assertEqual(
            OutboundEmail.query.order_by(OutboundEmail.id.desc()).first().message_id,
            reply.reply_message_id,
        )

    @patch(
        "services.manual_replies.send_profile_message",
        side_effect=TimeoutError("provider-secret-must-not-leak"),
    )
    def test_uncertain_manual_reply_is_never_retried(self, send):
        reply = EmailReply(
            lead=self.lead,
            campaign_recipient=self.recipient,
            sender_profile=self.profile,
            from_email=self.contact.value,
            subject="Re: Web",
            text_body="Prosím o viac informácií.",
            imap_message_id="<manual-timeout@example.test>",
            received_at=self.now,
        )
        db.session.add(reply)
        db.session.commit()

        first = self.client.post(
            f"/reply/{reply.id}/send", data={"reply_body": "Ďakujem."},
            follow_redirects=True,
        )
        second = self.client.post(
            f"/reply/{reply.id}/send", data={"reply_body": "Ďakujem."},
            follow_redirects=True,
        )

        send.assert_called_once()
        self.assertEqual(reply.reply_delivery_status, "unknown")
        self.assertIsNone(reply.reply_sent_at)
        self.assertEqual(OutboundEmail.query.count(), 1)
        self.assertNotIn("provider-secret", first.get_data(as_text=True))
        self.assertNotIn("provider-secret", second.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
