import unittest
from datetime import timedelta
from unittest.mock import patch

from app import create_app
from extensions import db
from models import Campaign, CampaignRecipient, Company, CompanyContact, OutboundEmail
from services.campaign_automation import run_campaign_automation
from services.campaign_delivery import (
    _claim_recipient,
    acquire_campaign_delivery_lock,
    delivery_slots_used_today,
    naive_utcnow,
    release_campaign_delivery_lock,
    send_campaign_recipients,
)


class CampaignDeliveryLockingTests(unittest.TestCase):
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

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def add_campaign(self, daily_limit=2, automation_enabled=False):
        campaign = Campaign(
            name="Súbežná kampaň",
            offer_type="service",
            offer_description="Test bezpečného odoslania",
            subject_template="Predmet",
            body_template="Text",
            status="active",
            daily_limit=daily_limit,
            automation_enabled=automation_enabled,
            targeting_profile={
                "nace_keywords": ["4321"],
                "company_keywords": [],
            },
            target_total=10,
            batch_size=2,
        )
        db.session.add(campaign)
        db.session.commit()
        return campaign

    def add_recipient(
        self,
        campaign,
        number,
        status="approved",
        contact_verified=None,
    ):
        company = Company(
            official_name=f"Firma {number}",
            ico=f"9000000{number}",
            sk_nace_code="4321",
        )
        contact = None
        if contact_verified is not None:
            contact = CompanyContact(
                company=company,
                contact_type="email",
                value=f"kontakt{number}@example.com",
                source_type="test",
                is_verified=contact_verified,
            )
        recipient = CampaignRecipient(
            campaign=campaign,
            company=company,
            contact=contact,
            recipient_email=(
                contact.value if contact is not None else f"kontakt{number}@example.test"
            ),
            subject="Test",
            body="Testovacia správa",
            status=status,
            approved_at=naive_utcnow(),
        )
        db.session.add_all([company, recipient])
        db.session.commit()
        return recipient

    def test_campaign_lock_has_owner_token_and_recovers_only_when_stale(self):
        campaign = self.add_campaign()

        first_token = acquire_campaign_delivery_lock(campaign.id)

        self.assertIsNotNone(first_token)
        self.assertIsNone(acquire_campaign_delivery_lock(campaign.id))

        campaign.delivery_locked_at = naive_utcnow() - timedelta(hours=2)
        db.session.commit()
        second_token = acquire_campaign_delivery_lock(campaign.id)

        self.assertIsNotNone(second_token)
        self.assertNotEqual(first_token, second_token)
        self.assertFalse(release_campaign_delivery_lock(campaign.id, first_token))
        self.assertTrue(release_campaign_delivery_lock(campaign.id, second_token))

    def test_recipient_claim_is_atomic_and_cannot_be_claimed_twice(self):
        campaign = self.add_campaign()
        recipient = self.add_recipient(campaign, 1)

        first_claim = _claim_recipient(recipient.id)
        second_claim = _claim_recipient(recipient.id)

        self.assertIsNotNone(first_claim)
        self.assertIsNone(second_claim)
        self.assertEqual(
            db.session.get(CampaignRecipient, recipient.id).status,
            "sending",
        )
        self.assertIsNotNone(first_claim.sending_started_at)

    @patch("services.campaign_delivery.mail.send")
    def test_daily_limit_counts_reserved_or_uncertain_attempts(self, send):
        campaign = self.add_campaign(daily_limit=1)
        reserved = self.add_recipient(campaign, 1, status="sending")
        reserved.sending_started_at = naive_utcnow()
        self.add_recipient(campaign, 2)
        db.session.commit()

        result = send_campaign_recipients(campaign, 1)

        self.assertEqual(delivery_slots_used_today(campaign), 1)
        self.assertEqual(result["sent"], 0)
        self.assertIn("Denný limit", result["message"])
        send.assert_not_called()

    @patch("services.campaign_delivery.mail.send")
    def test_busy_campaign_lock_blocks_manual_and_automation_delivery(self, send):
        campaign = self.add_campaign(automation_enabled=True)
        self.add_recipient(campaign, 1)
        token = acquire_campaign_delivery_lock(campaign.id)
        try:
            manual = send_campaign_recipients(campaign, 1)
            automated = run_campaign_automation(campaign, force=True)
        finally:
            release_campaign_delivery_lock(campaign.id, token)

        self.assertTrue(manual["locked"])
        self.assertIn("iný proces", manual["message"])
        self.assertIn("iný proces", automated["skipped"])
        send.assert_not_called()
        self.assertEqual(
            CampaignRecipient.query.one().status,
            "approved",
        )

    @patch("services.campaign_delivery.mail.send")
    def test_automatic_delivery_requires_verified_contact_but_manual_does_not(self, send):
        automated_campaign = self.add_campaign(automation_enabled=True)
        automated = self.add_recipient(
            automated_campaign,
            1,
            contact_verified=False,
        )
        manual_campaign = self.add_campaign(automation_enabled=False)
        manual = self.add_recipient(
            manual_campaign,
            2,
            contact_verified=False,
        )

        automated_result = send_campaign_recipients(automated_campaign, 1)
        manual_result = send_campaign_recipients(manual_campaign, 1)

        db.session.expire_all()
        automated = db.session.get(CampaignRecipient, automated.id)
        manual = db.session.get(CampaignRecipient, manual.id)
        self.assertEqual(automated_result["unverified"], 1)
        self.assertEqual(automated.status, "draft")
        self.assertIsNone(automated.approved_at)
        self.assertIn("platný a overený", automated.last_error)
        self.assertIsNone(automated.sending_started_at)
        self.assertEqual(manual_result["sent"], 1)
        self.assertEqual(manual.status, "sent")
        send.assert_called_once()

    @patch("services.campaign_delivery.mail.send")
    def test_database_error_after_smtp_keeps_recipient_in_safe_sending_state(self, send):
        campaign = self.add_campaign(daily_limit=1)
        recipient = self.add_recipient(campaign, 1)
        token = acquire_campaign_delivery_lock(campaign.id)
        original_add = db.session.add

        def fail_when_recording_outbound_email(instance, *args, **kwargs):
            if isinstance(instance, OutboundEmail):
                raise RuntimeError("simulované zlyhanie databázy")
            return original_add(instance, *args, **kwargs)

        try:
            with patch.object(
                db.session,
                "add",
                side_effect=fail_when_recording_outbound_email,
            ):
                result = send_campaign_recipients(
                    campaign,
                    1,
                    delivery_lock_token=token,
                )
        finally:
            release_campaign_delivery_lock(campaign.id, token)

        db.session.expire_all()
        stored = db.session.get(CampaignRecipient, recipient.id)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(stored.status, "sending")
        self.assertIn("mohol byť odoslaný", stored.last_error)
        self.assertIsNotNone(stored.sending_started_at)
        self.assertEqual(delivery_slots_used_today(campaign), 1)
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
