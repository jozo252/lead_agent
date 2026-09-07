"""Isolated UI-to-delivery checks. All SMTP is suppressed and IMAP/search mocked."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import create_app
from extensions import db, mail
from models import Campaign, CampaignFollowUp, CampaignRecipient, Company, CompanyContact, EmailReply, OutboundEmail, SenderProfile, Suppression
from services.campaign_followups import run_due_followups
from services.campaign_readiness import campaign_delivery_issues
from services.campaigns import OPT_OUT_FOOTER


class CampaignWorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        # A mistaken transport call must fail the test, never reach a server.
        for target in ("services.sender_profiles.smtplib.SMTP", "services.sender_profiles.smtplib.SMTP_SSL", "services.sender_profiles.imaplib.IMAP4_SSL", "requests.sessions.Session.request"):
            guard = patch(target, side_effect=AssertionError("Unexpected external network call"))
            guard.start()
            self.addCleanup(guard.stop)
        self.app = create_app({
            "TESTING": True, "SECRET_KEY": "test-only", "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite://", "MAIL_SUPPRESS_SEND": True,
            "MAIL_DEFAULT_SENDER": "legacy@example.test", "IMAP_USERNAME": "legacy@example.test",
            "SENDER_PROFILE_SETTINGS": {
                "WEBS": self.settings("webs@example.test"),
                "ELEKTRO": self.settings("elektro@example.test"),
            },
        })
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()
        self.assertEqual(self.client.post("/sender-profiles/prepare").status_code, 302)
        self.profile = SenderProfile.query.filter_by(config_key="WEBS").one()
        self.other_profile = SenderProfile.query.filter_by(config_key="ELEKTRO").one()
        for profile, name, address in ((self.profile, "Web tím", "webs@example.test"), (self.other_profile, "Elektro tím", "elektro@example.test")):
            self.client.post(f"/sender-profiles/{profile.id}", data={
                "sender_name": name, "sender_email": address,
                "signature": f"S pozdravom\n{name}", "enabled": "on",
            })
            self.assertTrue(profile.enabled)

    @staticmethod
    def settings(address):
        return {
            "MAIL_SERVER": "smtp.example.test", "MAIL_PORT": 587, "MAIL_USE_TLS": True,
            "MAIL_USERNAME": address, "MAIL_PASSWORD": "dummy-smtp-secret",
            "IMAP_SERVER": "imap.example.test", "IMAP_PORT": 993,
            "IMAP_USERNAME": address, "IMAP_PASSWORD": "dummy-imap-secret",
        }

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def create_campaign(self):
        self.campaign = Campaign(
            name="Malé stolárstva bez nájdeného webu", business_line="software",
            offer_type="service", offer_description="Tvorba jednoduchého firemného webu.",
            subject_template="Firemný web", body_template="Ponuka jednoduchého firemného webu.",
            targeting_profile={"nace_keywords": ["1623"]},
            automation_enabled=False, status="draft", daily_limit=5,
        )
        self.company = Company(official_name="Pekné Stolárstvo, s. r. o.", ico="12345678", municipality="Žilina", sk_nace_code="1623")
        self.contact = CompanyContact(company=self.company, contact_type="email", value="kontakt@example.com", source_type="test", is_verified=True)
        db.session.add_all([self.campaign, self.contact])
        db.session.commit()
        return self.campaign

    def workflow_data(self, **changes):
        data = {
            "sender_profile_id": str(self.profile.id), "require_no_website": "on",
            "follow_up_subject_template": "Re: Firemný web pre {company_name}",
            "follow_up_body_template": "Pripomínam ponuku jednoduchého webu.",
            "follow_up_days": "3", "follow_up_enabled": "on", "follow_up_approved": "on",
        }
        data.update(changes)
        return data

    def configure_campaign(self, **changes):
        return self.client.post(f"/campaigns/{self.campaign.id}/workflow-settings", data=self.workflow_data(**changes))

    def verify_website(self):
        payload = {"web": {"results": [{
            "url": "https://finstat.sk/12345678", "title": self.company.official_name,
            "description": "Žilina. IČO 12345678.",
        }]}}
        with patch("services.website_presence.brave_web_search", return_value=payload) as search:
            response = self.client.post(f"/campaigns/{self.campaign.id}/check-websites")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(search.call_count, 3)
        self.assertEqual(self.company.website_check.status, "not_found")

    def add_and_approve(self):
        self.client.post(f"/campaigns/{self.campaign.id}/add-filtered", data={"q": "12345678", "max_recipients": "1"})
        self.recipient = CampaignRecipient.query.one()
        self.client.post(f"/campaigns/{self.campaign.id}/recipients/{self.recipient.id}", data={
            "action": "approve", "subject": self.recipient.subject, "body": self.recipient.body,
        })
        self.assertEqual(self.recipient.status, "approved")

    def setup_ready_recipient(self):
        self.create_campaign()
        self.configure_campaign()
        self.assertTrue(self.campaign.follow_up_enabled)
        self.verify_website()
        self.add_and_approve()

    def send_first(self):
        with mail.record_messages() as messages:
            response = self.client.post(f"/campaigns/{self.campaign.id}/send", data={"batch_size": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.recipient.status, "sent")
        self.original = OutboundEmail.query.one()
        self.followup = CampaignFollowUp.query.one()
        return messages[0]

    def make_due(self):
        sent = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=4)
        self.original.sent_at = sent
        self.recipient.sent_at = sent
        self.original.lead.last_contacted_at = sent
        self.followup.due_at = sent + timedelta(days=3)
        self.original.lead.next_follow_up_at = self.followup.due_at
        db.session.commit()

    def incoming(self, body="Ďakujem, mám záujem."):
        return {
            "message_id": "<reply-customer@example.test>", "from_email": self.original.recipient,
            "from_name": "Zákazník", "subject": "Re: Firemný web", "body": body,
            "received_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "thread_message_ids": [self.original.message_id],
        }

    def test_three_profiles_are_explicit_idempotent_and_ui_hides_secrets(self):
        with mail.record_messages() as messages:
            self.client.post("/sender-profiles/prepare")
            page = self.client.get("/sender-profiles")
        self.assertEqual(SenderProfile.query.count(), 3)
        self.assertFalse(SenderProfile.query.filter_by(config_key="MG_STAV").one().enabled)
        self.assertEqual(page.status_code, 200)
        self.assertIn("M&amp;G-STAV", page.get_data(as_text=True))
        self.assertNotIn("dummy-smtp-secret", page.get_data(as_text=True))
        self.assertNotIn("dummy-imap-secret", page.get_data(as_text=True))
        self.assertEqual(messages, [])

    def test_end_to_end_verified_selection_first_send_one_threaded_followup(self):
        self.setup_ready_recipient()
        first = self.send_first()
        self.assertEqual(first.sender, ("Web tím", "webs@example.test"))
        self.assertEqual(first.reply_to, "webs@example.test")
        self.assertEqual(first.body.count(self.profile.signature), 1)
        self.assertTrue(first.body.endswith(OPT_OUT_FOOTER))
        self.assertEqual(self.original.body, first.body)
        self.assertEqual(self.original.sender_profile_id, self.profile.id)
        self.make_due()
        with patch("services.campaign_followups.fetch_profile_messages", return_value=[]) as inbox, mail.record_messages() as messages:
            result = run_due_followups(dry_run=False)
            repeated = run_due_followups(dry_run=False)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(repeated["selected"], 0)
        self.assertEqual(len(messages), 1)
        self.assertEqual(inbox.call_count, 1)
        self.assertEqual(messages[0].sender, first.sender)
        self.assertEqual(messages[0].extra_headers["In-Reply-To"], self.original.message_id)
        self.assertEqual(messages[0].extra_headers["References"], self.original.message_id)
        self.assertEqual(messages[0].body.count(self.profile.signature), 1)
        self.assertEqual(OutboundEmail.query.count(), 2)
        self.assertEqual(self.followup.status, "sent")
        self.assertEqual(self.app.config["MAIL_DEFAULT_SENDER"], "legacy@example.test")
        self.assertEqual(self.client.get(f"/campaigns/{self.campaign.id}").status_code, 200)

    def test_opt_out_cancels_reminder_and_blocks_manual_reply(self):
        self.setup_ready_recipient()
        self.send_first()
        self.make_due()
        with patch("services.campaign_followups.fetch_profile_messages", return_value=[self.incoming("neposielať")]), mail.record_messages() as messages:
            result = run_due_followups(dry_run=False)
            reply = EmailReply.query.one()
            self.client.post(f"/reply/{reply.id}/send", data={"reply_body": "Ďalšia ponuka"})
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(self.followup.status, "cancelled")
        self.assertGreater(Suppression.query.count(), 0)
        self.assertEqual(messages, [])
        self.assertEqual(OutboundEmail.query.count(), 1)

    def test_inbox_profile_selector_and_manual_reply_keep_account_and_thread(self):
        self.setup_ready_recipient()
        self.send_first()
        with patch("services.campaign_followups.fetch_profile_messages", return_value=[self.incoming()]) as fetch:
            response = self.client.post("/inbox/sync", data={"sender_profile_id": str(self.profile.id)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(fetch.call_args.args[0].id, self.profile.id)
        reply = EmailReply.query.one()
        self.assertEqual(reply.sender_profile_id, self.profile.id)
        with mail.record_messages() as messages:
            self.client.post(f"/reply/{reply.id}/send", data={"reply_body": "Ďakujem, pripravím rozsah."})
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].sender, ("Web tím", "webs@example.test"))
        self.assertEqual(messages[0].extra_headers["In-Reply-To"], reply.imap_message_id)
        self.assertEqual(OutboundEmail.query.order_by(OutboundEmail.id.desc()).first().sender_profile_id, self.profile.id)
        self.assertEqual(self.followup.status, "cancelled")
        inbox_page = self.client.get("/inbox")
        self.assertEqual(inbox_page.status_code, 200)
        self.assertIn('name="sender_profile_id"', inbox_page.get_data(as_text=True))

    def test_followup_settings_require_explicit_approval_and_existing_profile(self):
        self.create_campaign()
        self.configure_campaign(follow_up_approved="")
        self.assertFalse(self.campaign.follow_up_enabled)
        self.assertIsNone(self.campaign.sender_profile_id)
        self.configure_campaign(sender_profile_id="999999")
        self.assertFalse(self.campaign.follow_up_enabled)
        self.assertIsNone(self.campaign.follow_up_approved_at)
        self.assertEqual(CampaignFollowUp.query.count(), 0)

    def test_missing_configuration_or_approval_blocks_activation(self):
        self.create_campaign()
        self.configure_campaign()
        self.campaign.follow_up_approved_at = None
        db.session.commit()
        self.client.post(f"/campaigns/{self.campaign.id}/status", data={"status": "active"})
        self.assertEqual(self.campaign.status, "draft")
        self.campaign.follow_up_approved_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()
        self.app.config["SENDER_PROFILE_SETTINGS"]["WEBS"] = {}
        self.client.post(f"/campaigns/{self.campaign.id}/status", data={"status": "active"})
        self.assertEqual(self.campaign.status, "draft")

    def test_unknown_website_cannot_be_added_or_forced_into_delivery(self):
        self.create_campaign()
        self.configure_campaign()
        self.client.post(f"/campaigns/{self.campaign.id}/add-filtered", data={"q": "12345678"})
        self.assertEqual(CampaignRecipient.query.count(), 0)
        recipient = CampaignRecipient(campaign=self.campaign, company=self.company, contact=self.contact, recipient_email=self.contact.value, subject="Test", body="Test", status="approved")
        db.session.add(recipient)
        db.session.commit()
        with mail.record_messages() as messages:
            self.client.post(f"/campaigns/{self.campaign.id}/send", data={"batch_size": "1"})
        self.assertEqual(messages, [])
        self.assertEqual(recipient.status, "draft")
        self.assertEqual(OutboundEmail.query.count(), 0)

    def test_stale_website_cannot_be_manually_reapproved(self):
        self.setup_ready_recipient()
        self.recipient.status = "draft"
        self.company.website_check.checked_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=31)
        db.session.commit()
        self.client.post(f"/campaigns/{self.campaign.id}/recipients/{self.recipient.id}", data={"action": "approve", "subject": "Test", "body": "Test"})
        self.assertEqual(self.recipient.status, "draft")

    def test_signature_change_cancels_pending_and_revokes_approval(self):
        self.setup_ready_recipient()
        self.send_first()
        self.client.post(f"/campaigns/{self.campaign.id}/status", data={"status": "paused"})
        self.client.post(f"/sender-profiles/{self.profile.id}", data={
            "sender_email": self.profile.sender_email, "sender_name": self.profile.sender_name,
            "signature": "Nový schválený podpis", "enabled": "on",
        })
        self.assertEqual(self.followup.status, "cancelled")
        self.assertFalse(self.campaign.follow_up_enabled)
        self.assertIsNone(self.campaign.follow_up_approved_at)
        self.assertIsNone(self.original.lead.next_follow_up_at)

    def test_signature_change_revokes_unsent_recipient_approval(self):
        self.setup_ready_recipient()

        self.client.post(f"/sender-profiles/{self.profile.id}", data={
            "sender_email": self.profile.sender_email,
            "sender_name": self.profile.sender_name,
            "signature": "Nový schválený podpis",
            "enabled": "on",
        })

        db.session.refresh(self.recipient)
        self.assertEqual(self.recipient.status, "draft")
        self.assertIsNone(self.recipient.approved_at)
        self.assertIn("nové schválenie", self.recipient.last_error)
        with mail.record_messages() as messages:
            self.client.post(
                f"/campaigns/{self.campaign.id}/send",
                data={"batch_size": "1"},
            )
        self.assertEqual(messages, [])

    def test_historical_sender_email_cannot_be_changed(self):
        self.setup_ready_recipient()
        self.send_first()
        self.client.post(f"/campaigns/{self.campaign.id}/status", data={"status": "paused"})
        self.client.post(f"/sender-profiles/{self.profile.id}", data={
            "sender_email": "changed@example.test", "sender_name": self.profile.sender_name,
            "signature": self.profile.signature,
        })
        self.assertEqual(self.profile.sender_email, "webs@example.test")
        self.assertEqual(self.original.sender_profile_id, self.profile.id)
        self.assertEqual(self.followup.status, "scheduled")

    def test_sending_or_unknown_recipient_cannot_be_edited_or_reapproved(self):
        self.setup_ready_recipient()
        body = self.recipient.body
        for status in ("sending", "unknown"):
            for action in ("save", "approve"):
                with self.subTest(status=status, action=action):
                    self.recipient.status = status
                    db.session.commit()
                    self.client.post(f"/campaigns/{self.campaign.id}/recipients/{self.recipient.id}", data={"action": action, "subject": "Zmenené", "body": "Zmenené"})
                    self.assertEqual(self.recipient.status, status)
                    self.assertEqual(self.recipient.body, body)
        page = self.client.get(f"/campaigns/{self.campaign.id}").get_data(as_text=True)
        self.assertNotIn(f'action="/campaigns/{self.campaign.id}/recipients/{self.recipient.id}"', page)

    def test_legacy_inbox_used_profile_mailbox_is_blocked_before_network(self):
        self.setup_ready_recipient()
        self.send_first()
        self.app.config["IMAP_USERNAME"] = "webs@example.test"
        with patch("routes.fetch_inbox_messages") as legacy:
            response = self.client.post("/inbox/sync")
        self.assertEqual(response.status_code, 302)
        legacy.assert_not_called()
        self.assertEqual(EmailReply.query.count(), 0)

    def test_profile_switch_cancels_old_pending_without_reassigning_history(self):
        self.setup_ready_recipient()
        self.send_first()
        self.client.post(f"/campaigns/{self.campaign.id}/status", data={"status": "paused"})
        self.configure_campaign(sender_profile_id=str(self.other_profile.id))
        self.assertEqual(self.campaign.sender_profile_id, self.other_profile.id)
        self.assertEqual(self.original.sender_profile_id, self.profile.id)
        self.assertEqual(self.followup.sender_profile_id, self.profile.id)
        self.assertEqual(self.followup.status, "cancelled")

    def test_profile_switch_revokes_unsent_recipient_approval(self):
        self.setup_ready_recipient()

        self.configure_campaign(sender_profile_id=str(self.other_profile.id))

        db.session.refresh(self.recipient)
        self.assertEqual(self.campaign.sender_profile_id, self.other_profile.id)
        self.assertEqual(self.recipient.status, "draft")
        self.assertIsNone(self.recipient.approved_at)
        self.assertIn("nové schválenie", self.recipient.last_error)
        with mail.record_messages() as messages:
            self.client.post(
                f"/campaigns/{self.campaign.id}/send",
                data={"batch_size": "1"},
            )
        self.assertEqual(messages, [])

    def test_active_or_busy_campaign_settings_cannot_change(self):
        self.create_campaign()
        self.configure_campaign()
        for active, token in (("active", None), ("paused", "busy-worker")):
            with self.subTest(status=active, token=token):
                self.campaign.status, self.campaign.delivery_lock_token = active, token
                db.session.commit()
                self.configure_campaign(sender_profile_id=str(self.other_profile.id))
                self.assertEqual(self.campaign.sender_profile_id, self.profile.id)

    def test_disabled_profile_blocks_initial_send_and_inbox_import(self):
        self.setup_ready_recipient()
        self.client.post(f"/sender-profiles/{self.profile.id}", data={"disable": "yes"})
        with patch("services.campaign_followups.fetch_profile_messages") as fetch, mail.record_messages() as messages:
            self.client.post(f"/campaigns/{self.campaign.id}/send", data={"batch_size": "1"})
            self.client.post("/inbox/sync", data={"sender_profile_id": str(self.profile.id)})
        self.assertEqual(messages, [])
        fetch.assert_not_called()
        self.assertEqual(self.recipient.status, "approved")
        self.assertEqual(OutboundEmail.query.count(), 0)

    def test_three_campaign_presets_are_draft_idempotent_and_do_not_send(self):
        from services.campaign_automation import run_due_campaigns

        with mail.record_messages() as messages:
            first = self.client.post("/campaigns/prepare-three")
            second = self.client.post("/campaigns/prepare-three")
            due = run_due_campaigns()
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(Campaign.query.count(), 3)
        campaigns = {campaign.business_line: campaign for campaign in Campaign.query.all()}
        self.assertEqual(set(campaigns), {"software", "electrical", "construction"})
        self.assertEqual(campaigns["software"].sender_profile.config_key, "WEBS")
        self.assertEqual(campaigns["electrical"].sender_profile.config_key, "ELEKTRO")
        self.assertEqual(campaigns["construction"].sender_profile.config_key, "MG_STAV")
        self.assertTrue(campaigns["software"].require_no_website)
        for campaign in campaigns.values():
            self.assertEqual(campaign.status, "draft")
            self.assertTrue(campaign.automation_enabled)
            self.assertFalse(campaign.scout_enabled)
            self.assertFalse(campaign.follow_up_enabled)
            self.assertIsNone(campaign.last_automation_run_at)
            self.assertTrue(campaign_delivery_issues(campaign))
        self.assertEqual(due, [])
        self.assertEqual(messages, [])

    def test_profiled_history_blocks_both_legacy_manual_send_routes(self):
        self.setup_ready_recipient()
        self.send_first()
        original_body = self.original.body
        with patch("routes.mail.send") as legacy_send:
            company_response = self.client.post(f"/companies/{self.company.id}/send-outreach", data={
                "email": self.contact.value, "subject": "Opätovná ponuka", "message": "Zmena identity nie je povolená.",
            })
            lead_response = self.client.post(f"/lead/{self.original.lead_id}/send-email", data={
                "email_subject": "Opätovná ponuka", "suggested_message": "Zmena identity nie je povolená.",
            })
        self.assertEqual((company_response.status_code, lead_response.status_code), (302, 302))
        legacy_send.assert_not_called()
        self.assertEqual(OutboundEmail.query.count(), 1)
        self.assertEqual(self.original.body, original_body)

    def test_unfinished_preset_cannot_activate_with_ready_sender_profile(self):
        self.client.post("/campaigns/prepare-three")
        campaign = Campaign.query.filter_by(business_line="software").one()
        self.assertTrue(campaign.sender_profile.enabled)
        placeholder = campaign.body_template
        for body, targeting in (
            (placeholder, {"nace_keywords": ["1623"]}),
            ("Konkrétna ponuka webovej stránky.", {"nace_keywords": [], "company_keywords": []}),
        ):
            with self.subTest(body=body, targeting=targeting), mail.record_messages() as messages:
                campaign.body_template = body
                campaign.targeting_profile = targeting
                db.session.commit()
                response = self.client.post(f"/campaigns/{campaign.id}/status", data={"status": "active"})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(campaign.status, "draft")
                self.assertIsNone(campaign.activated_at)
                self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main()
