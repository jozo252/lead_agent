import unittest
from datetime import datetime
from unittest.mock import patch

from app import create_app
from extensions import db
from models import (
    Campaign,
    CampaignRecipient,
    Company,
    EmailReply,
    Lead,
    OutboundEmail,
    Suppression,
)
from services.campaigns import is_explicit_opt_out_reply


class CampaignOptOutTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "SQLALCHEMY_DATABASE_URI": "sqlite://",
                "WTF_CSRF_ENABLED": False,
                "MAIL_SUPPRESS_SEND": True,
            }
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()

        company = Company(official_name="Opt-out Firma", ico="90000001")
        campaign = Campaign(
            name="Opt-out test",
            offer_type="service",
            offer_description="Testovacia ponuka",
            subject_template="Test",
            body_template="Testovacia správa",
        )
        db.session.add_all([company, campaign])
        db.session.flush()

        self.lead = Lead(
            company_id=company.id,
            company_name=company.official_name,
            email="kontakt@optout.test",
            status="Kontaktovaný",
            last_contacted_at=datetime(2026, 8, 22, 10, 0),
        )
        self.recipient = CampaignRecipient(
            campaign=campaign,
            company=company,
            recipient_email="kontakt@optout.test",
            subject="Test",
            body="Testovacia správa",
            status="sent",
            sent_at=datetime(2026, 8, 22, 10, 0),
        )
        db.session.add_all([self.lead, self.recipient])
        db.session.flush()
        db.session.add(
            OutboundEmail(
                lead=self.lead,
                campaign_recipient=self.recipient,
                message_id="<outbound-optout@example.test>",
                recipient="kontakt@optout.test",
                subject="Test",
                body="Testovacia správa",
                sent_at=datetime(2026, 8, 22, 10, 0),
            )
        )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_detector_accepts_only_short_explicit_opt_outs(self):
        for body in (
            "neposielať",
            "Prosím odhlásiť ma.",
            "Nemám záujem, už ma nekontaktujte.",
            "Dobrý deň, prosím neposielajte mi ďalšie e-maily. Ďakujem.",
        ):
            with self.subTest(body=body):
                self.assertTrue(is_explicit_opt_out_reply(body))

        self.assertFalse(
            is_explicit_opt_out_reply(
                "Mám záujem, pošlite viac informácií.\n\n"
                "On Sat, 22 Aug 2026 Adam wrote:\n"
                "Ak si neželáte ďalšie správy, stačí odpovedať „neposielať“."
            )
        )

    @patch("routes.check_reply_from_sender")
    def test_manual_reply_check_creates_suppression_and_opts_recipient_out(
        self,
        check_reply,
    ):
        check_reply.return_value = {
            "from": "Opt-out Firma <kontakt@optout.test>",
            "subject": "Re: Test",
            "received_at": datetime(2026, 8, 23, 9, 0),
            "body": "Prosím odhlásiť ma.",
            "message_id": "<manual-optout@example.test>",
            "thread_message_ids": ["<outbound-optout@example.test>"],
        }

        response = self.client.post(f"/lead/{self.lead.id}/check-reply")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.recipient.status, "opted_out")
        self.assertIsNotNone(self.recipient.replied_at)
        suppression = Suppression.query.one()
        self.assertEqual(suppression.scope, "email")
        self.assertEqual(suppression.value, "kontakt@optout.test")
        self.assertEqual(EmailReply.query.count(), 1)

    @patch("routes.fetch_inbox_messages")
    def test_inbox_sync_creates_suppression_and_opts_recipient_out(self, fetch):
        fetch.return_value = [
            {
                "from_email": "kontakt@optout.test",
                "from_name": "Opt-out Firma",
                "subject": "Re: Test",
                "body": "Nemám záujem, už ma nekontaktujte.",
                "message_id": "<sync-optout@example.test>",
                "received_at": datetime(2026, 8, 23, 9, 30),
                "thread_message_ids": ["<outbound-optout@example.test>"],
            }
        ]

        response = self.client.post("/inbox/sync")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.recipient.status, "opted_out")
        self.assertEqual(Suppression.query.one().value, "kontakt@optout.test")

    @patch("routes.fetch_inbox_messages")
    def test_inbox_sync_ignores_opt_out_footer_in_quoted_original(self, fetch):
        fetch.return_value = [
            {
                "from_email": "kontakt@optout.test",
                "from_name": "Opt-out Firma",
                "subject": "Re: Test",
                "body": (
                    "Mám záujem, pošlite viac informácií.\n\n"
                    "Dňa 22. 8. 2026 Adam napísal:\n"
                    "Ak si neželáte ďalšie správy, stačí odpovedať „neposielať“."
                ),
                "message_id": "<sync-positive@example.test>",
                "received_at": datetime(2026, 8, 23, 10, 0),
                "thread_message_ids": ["<outbound-optout@example.test>"],
            }
        ]

        self.client.post("/inbox/sync")

        self.assertEqual(self.recipient.status, "replied")
        self.assertEqual(Suppression.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
