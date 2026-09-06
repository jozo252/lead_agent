import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import create_app
from extensions import db
from models import (
    Campaign, CampaignFollowUp, CampaignRecipient, Company, CompanyContact, CompanyWebsiteCheck,
    EmailReply, Lead, OutboundEmail, SenderProfile, Suppression,
)
from services.campaign_delivery import acquire_campaign_delivery_lock
from services.campaign_followups import (
    _claim, run_due_followups, schedule_campaign_followup, sync_profile_inbox,
)
from services.campaigns import company_suppression_value


class CampaignFollowUpTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True, "SECRET_KEY": "test-only", "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite://", "MAIL_SUPPRESS_SEND": True,
            "MAIL_DEFAULT_SENDER": "legacy@example.com",
            "SENDER_PROFILE_SETTINGS": {"WEBS": self.settings("webs@example.com"),
                                        "ELEKTRO": self.settings("elektro@example.com")},
        })
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.now = datetime.now(timezone.utc).replace(tzinfo=None)
        self.profile = SenderProfile(name="Weby", config_key="WEBS", sender_name="Web tím",
                                     sender_email="webs@example.com", signature="Web tím", enabled=True)
        self.campaign = Campaign(
            name="Weby", offer_description="Web služby", subject_template="Web",
            body_template="Ponuka", sender_profile=self.profile, status="active", daily_limit=5,
            follow_up_enabled=True, follow_up_days=3, follow_up_approved_at=self.now,
            follow_up_subject_template="Re: Web pre {company_name}",
            follow_up_body_template="Pripomínam správu pre {company_name}.",
        )
        db.session.add(self.campaign)
        db.session.flush()
        self.row = self.add_due_followup("Firma Alpha", "100001", "kontakt@example.com")
        db.session.commit()

    @staticmethod
    def settings(address):
        return {"MAIL_SERVER": "smtp.example.com", "MAIL_PORT": 587, "MAIL_USE_TLS": True,
                "MAIL_USERNAME": address, "MAIL_PASSWORD": "test-only",
                "IMAP_SERVER": "imap.example.com", "IMAP_PORT": 993,
                "IMAP_USERNAME": address, "IMAP_PASSWORD": "test-only"}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def add_due_followup(self, name, ico, address):
        company = Company(official_name=name, ico=ico, municipality="Poprad")
        contact = CompanyContact(company=company, contact_type="email", value=address,
                                 source_type="test", is_verified=True)
        lead = Lead(company=company, company_name=name, email=address, status="Oslovený",
                    last_contacted_at=self.now - timedelta(days=4))
        recipient = CampaignRecipient(campaign=self.campaign, company=company, contact=contact,
                                      recipient_email=address, subject="Web", body="Ponuka", status="sent",
                                      sent_at=self.now - timedelta(days=4))
        original = OutboundEmail(lead=lead, campaign_recipient=recipient, sender_profile=self.profile,
                                 message_id=f"<original-{ico}@example.com>", recipient=address,
                                 subject="Web", body="Ponuka", sent_at=self.now - timedelta(days=4))
        db.session.add_all([contact, original])
        db.session.flush()
        return schedule_campaign_followup(self.campaign, recipient, original)

    def message(self, **changes):
        message = {"message_id": "<reply@example.com>", "from_email": "kontakt@example.com",
                   "from_name": "Firma", "subject": "Re: Web", "body": "Ďakujem, ozvem sa.",
                   "received_at": self.now - timedelta(days=1),
                   "thread_message_ids": [self.row.original_outbound.message_id]}
        message.update(changes)
        return message

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages", return_value=[])
    def test_dry_run_is_read_only_without_network(self, fetch, send):
        before = (self.row.status, self.profile.last_synced_at, self.campaign.delivery_lock_token)
        result = run_due_followups()
        self.assertEqual(result["eligible"], 1)
        self.assertTrue(result["dry_run"])
        self.assertEqual((self.row.status, self.profile.last_synced_at, self.campaign.delivery_lock_token), before)
        fetch.assert_not_called()
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages", return_value=[])
    def test_send_preserves_thread_and_never_sends_twice(self, fetch, send):
        def deliver(message, profile):
            self.assertEqual(db.session.get(CampaignFollowUp, self.row.id).status, "sending")
            self.assertIsNotNone(self.row.sending_started_at)
            self.assertEqual(message.msgId, self.row.message_id)
            message.body += "\n\nWeb tím"
        send.side_effect = deliver
        result = run_due_followups(dry_run=False)
        self.assertEqual(result["sent"], 1)
        message, profile = send.call_args.args
        self.assertEqual(profile.id, self.profile.id)
        self.assertEqual(message.extra_headers["In-Reply-To"], self.row.original_outbound.message_id)
        self.assertEqual(message.extra_headers["References"], self.row.original_outbound.message_id)
        self.assertEqual(self.row.status, "sent")
        self.assertIsNone(self.row.original_outbound.lead.next_follow_up_at)
        latest = OutboundEmail.query.order_by(OutboundEmail.id.desc()).first()
        self.assertEqual(latest.body, message.body)
        self.assertEqual(latest.sender_profile_id, self.profile.id)
        self.assertEqual(OutboundEmail.query.count(), 2)
        self.assertEqual(run_due_followups(dry_run=False)["selected"], 0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(fetch.call_count, 1)

    def test_schedule_is_unique_and_requires_explicit_approval(self):
        self.assertEqual(schedule_campaign_followup(self.campaign, self.row.campaign_recipient,
                                                   self.row.original_outbound).id, self.row.id)
        self.campaign.follow_up_enabled = False
        self.assertIsNone(schedule_campaign_followup(self.campaign, self.row.campaign_recipient,
                                                     self.row.original_outbound))
        self.campaign.follow_up_enabled = True
        self.campaign.follow_up_approved_at = None
        self.assertIsNone(schedule_campaign_followup(self.campaign, self.row.campaign_recipient,
                                                     self.row.original_outbound))
        self.assertEqual(CampaignFollowUp.query.count(), 1)

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_reply_is_saved_and_cancels_followup(self, fetch, send):
        fetch.return_value = [self.message()]
        result = run_due_followups(dry_run=False)
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(self.row.status, "cancelled")
        self.assertEqual(self.row.original_outbound.lead.status, "Odpovedal")
        self.assertEqual(EmailReply.query.one().sender_profile_id, self.profile.id)
        send.assert_not_called()
        self.assertIsNotNone(self.profile.last_synced_at)
        self.assertEqual(sync_profile_inbox(self.profile)["imported"], 0)
        self.assertEqual(EmailReply.query.count(), 1)

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_colleague_reply_in_original_thread_stops_reminder(self, fetch, send):
        fetch.return_value = [self.message(from_email="colleague@example.com")]
        result = run_due_followups(dry_run=False)
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(self.row.status, "cancelled")
        self.assertEqual(EmailReply.query.one().from_email, "colleague@example.com")
        self.assertEqual(EmailReply.query.one().lead_id, self.row.original_outbound.lead_id)
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_reply_referencing_multiple_leads_blocks_instead_of_guessing(self, fetch, send):
        second = self.add_due_followup("Beta", "100002", "beta@example.com")
        db.session.commit()
        fetch.return_value = [self.message(thread_message_ids=[
            self.row.original_outbound.message_id, second.original_outbound.message_id,
        ])]
        result = run_due_followups(dry_run=False)
        self.assertEqual(result["blocked"], 2)
        self.assertEqual(EmailReply.query.count(), 0)
        self.assertEqual(self.row.status, "scheduled")
        self.assertEqual(second.status, "scheduled")
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_unthreaded_reply_shared_by_multiple_leads_blocks_instead_of_guessing(self, fetch, send):
        second = self.add_due_followup("Beta", "100002", "kontakt@example.com")
        db.session.commit()
        fetch.return_value = [self.message(thread_message_ids=[])]
        result = run_due_followups(dry_run=False)
        self.assertEqual(result["blocked"], 2)
        self.assertEqual(EmailReply.query.count(), 0)
        self.assertEqual(self.row.status, "scheduled")
        self.assertEqual(second.status, "scheduled")
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_optout_blocks_followup_and_creates_suppression(self, fetch, send):
        fetch.return_value = [self.message(body="Prosím neposielať ďalšie správy.")]
        run_due_followups(dry_run=False)
        self.assertEqual(self.row.campaign_recipient.status, "opted_out")
        self.assertEqual(Suppression.query.one().value, "kontakt@example.com")
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_undated_reply_without_message_id_or_thread_still_blocks(self, fetch, send):
        fetch.return_value = [self.message(message_id=None, thread_message_ids=[], received_at=None)]
        run_due_followups(dry_run=False)
        self.assertEqual(self.row.status, "cancelled")
        self.assertTrue(EmailReply.query.one().imap_message_id.startswith("<missing-"))
        sync_profile_inbox(self.profile)
        self.assertEqual(EmailReply.query.count(), 1)
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_unrelated_older_message_does_not_count_as_reply(self, fetch, send):
        fetch.return_value = [self.message(thread_message_ids=[], received_at=self.now - timedelta(days=10))]
        result = run_due_followups(dry_run=False)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(EmailReply.query.count(), 0)

    @patch("services.campaign_followups.fetch_profile_messages")
    def test_profile_b_does_not_import_profile_a_reply(self, fetch):
        other = SenderProfile(name="Elektro", config_key="ELEKTRO", sender_name="Elektro",
                              sender_email="elektro@example.com", enabled=True)
        db.session.add(other)
        db.session.commit()
        fetch.return_value = [self.message()]
        result = sync_profile_inbox(other)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(EmailReply.query.count(), 0)
        self.assertEqual(self.row.status, "scheduled")
        self.assertEqual(self.row.original_outbound.lead.status, "Oslovený")

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages", side_effect=RuntimeError("PASSWORD=secret"))
    def test_incomplete_inbox_blocks_send_without_exposing_error(self, fetch, send):
        result = run_due_followups(dry_run=False)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(self.row.status, "scheduled")
        self.assertIsNone(self.profile.last_synced_at)
        self.assertNotIn("secret", self.profile.last_sync_error)
        self.assertNotIn("secret", str(result))
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message", side_effect=TimeoutError("secret"))
    @patch("services.campaign_followups.fetch_profile_messages", return_value=[])
    def test_smtp_timeout_is_unknown_and_not_retried(self, fetch, send):
        result = run_due_followups(dry_run=False)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(self.row.status, "unknown")
        self.assertNotIn("secret", self.row.last_error)
        self.assertIsNotNone(self.row.sending_started_at)
        self.assertEqual(run_due_followups(dry_run=False)["selected"], 0)
        self.assertEqual(send.call_count, 1)

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages", return_value=[])
    def test_database_failure_after_smtp_preserves_unknown_claim(self, fetch, send):
        original_commit = db.session.commit
        failed = [False]
        def commit():
            if send.called and not failed[0]:
                failed[0] = True
                raise RuntimeError("simulated SQL failure after SMTP")
            return original_commit()
        with patch.object(db.session, "commit", side_effect=commit):
            result = run_due_followups(dry_run=False)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(self.row.status, "unknown")
        self.assertEqual(OutboundEmail.query.count(), 1)
        self.assertEqual(run_due_followups(dry_run=False)["selected"], 0)
        send.assert_called_once()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages", return_value=[])
    def test_daily_cap_counts_followups_in_same_run(self, fetch, send):
        second = self.add_due_followup("Beta", "100002", "beta@example.com")
        self.campaign.daily_limit = 1
        db.session.commit()
        result = run_due_followups(dry_run=False)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(second.status, "scheduled")
        send.assert_called_once()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_delivery_lock_contention_prevents_network(self, fetch, send):
        token = acquire_campaign_delivery_lock(self.campaign.id)
        self.assertIsNotNone(token)
        result = run_due_followups(dry_run=False)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(self.campaign.delivery_lock_token, token)
        fetch.assert_not_called()
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_paused_campaign_and_disabled_profile_prevent_network(self, fetch, send):
        self.campaign.status = "paused"
        db.session.commit()
        self.assertEqual(run_due_followups(dry_run=False)["blocked"], 1)
        self.assertEqual(self.row.status, "scheduled")
        self.campaign.status = "active"
        self.profile.enabled = False
        db.session.commit()
        self.assertEqual(run_due_followups(dry_run=False)["blocked"], 1)
        fetch.assert_not_called()
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_changed_template_cancels_old_snapshot(self, fetch, send):
        self.campaign.follow_up_body_template = "Nová ponuka."
        db.session.commit()
        self.assertEqual(run_due_followups(dry_run=False)["cancelled"], 1)
        fetch.assert_not_called()
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_profile_change_cancels_without_wrong_sender(self, fetch, send):
        self.campaign.sender_profile = None
        db.session.commit()
        self.assertEqual(run_due_followups(dry_run=False)["cancelled"], 1)
        fetch.assert_not_called()
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_newer_manual_contact_cancels(self, fetch, send):
        self.row.original_outbound.lead.last_contacted_at = self.now - timedelta(days=1)
        db.session.commit()
        self.assertEqual(run_due_followups(dry_run=False)["cancelled"], 1)
        fetch.assert_not_called()
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_known_suppression_and_unverified_contact_prevent_network(self, fetch, send):
        db.session.add(Suppression(scope="email", value="kontakt@example.com"))
        db.session.commit()
        self.assertEqual(run_due_followups(dry_run=False)["suppressed"], 1)
        self.row.status = "scheduled"
        Suppression.query.delete()
        self.row.campaign_recipient.contact.is_verified = False
        db.session.commit()
        self.assertEqual(run_due_followups(dry_run=False)["cancelled"], 1)
        fetch.assert_not_called()
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_pause_during_sync_is_rechecked_before_claim(self, fetch, send):
        def inbox(*args, **kwargs):
            self.campaign.status = "paused"
            db.session.commit()
            return []
        fetch.side_effect = inbox
        self.assertEqual(run_due_followups(dry_run=False)["blocked"], 1)
        self.assertEqual(self.row.status, "scheduled")
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages", return_value=[])
    def test_reply_arriving_after_final_gate_prevents_atomic_claim(self, fetch, send):
        def concurrent_reply(row):
            row.campaign_recipient.status = "replied"
            row.campaign_recipient.replied_at = self.now
            row.original_outbound.lead.status = "Odpovedal"
            db.session.commit()
            return _claim(row)
        with patch("services.campaign_followups._claim", side_effect=concurrent_reply):
            result = run_due_followups(dry_run=False)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(self.row.status, "scheduled")
        self.assertIsNone(self.row.sending_started_at)
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages", return_value=[])
    def test_reply_record_added_after_gate_prevents_atomic_claim(self, fetch, send):
        def concurrent_reply(row):
            db.session.add(EmailReply(lead=row.original_outbound.lead, received_at=self.now,
                                      from_email="kontakt@example.com", text_body="Ozvem sa."))
            db.session.commit()
            return _claim(row)
        with patch("services.campaign_followups._claim", side_effect=concurrent_reply):
            result = run_due_followups(dry_run=False)
        self.assertEqual(result["blocked"], 1)
        self.assertIsNone(self.row.sending_started_at)
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages", return_value=[])
    def test_manual_outbound_added_after_gate_prevents_atomic_claim(self, fetch, send):
        def concurrent_send(row):
            db.session.add(OutboundEmail(
                lead=row.original_outbound.lead, sender_profile=self.profile,
                recipient=row.original_outbound.recipient, message_id="<manual@example.com>",
                subject="Osobná odpoveď", body="Dohodnuté.", sent_at=self.now,
            ))
            db.session.commit()
            return _claim(row)
        with patch("services.campaign_followups._claim", side_effect=concurrent_send):
            result = run_due_followups(dry_run=False)
        self.assertEqual(result["blocked"], 1)
        self.assertIsNone(self.row.sending_started_at)
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages", return_value=[])
    def test_suppression_added_after_gate_prevents_atomic_claim(self, fetch, send):
        values = {"email": "kontakt@example.com", "domain": "example.com",
                  "company": company_suppression_value(self.row.campaign_recipient.company)}
        for scope, value in values.items():
            with self.subTest(scope=scope):
                def concurrent_optout(row):
                    db.session.add(Suppression(scope=scope, value=value))
                    db.session.commit()
                    return _claim(row)
                with patch("services.campaign_followups._claim", side_effect=concurrent_optout):
                    result = run_due_followups(dry_run=False)
                self.assertEqual(result["blocked"], 1)
                self.assertIsNone(self.row.sending_started_at)
                Suppression.query.delete()
                db.session.commit()
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages", return_value=[])
    def test_contact_verification_revoked_after_gate_prevents_atomic_claim(self, fetch, send):
        def concurrent_change(row):
            row.campaign_recipient.contact.is_verified = False
            db.session.commit()
            return _claim(row)
        with patch("services.campaign_followups._claim", side_effect=concurrent_change):
            result = run_due_followups(dry_run=False)
        self.assertEqual(result["blocked"], 1)
        self.assertIsNone(self.row.sending_started_at)
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages", return_value=[])
    def test_verified_contact_replaced_after_gate_prevents_atomic_claim(self, fetch, send):
        def concurrent_change(row):
            row.campaign_recipient.contact.value = "replacement@example.com"
            row.campaign_recipient.recipient_email = "replacement@example.com"
            db.session.commit()
            return _claim(row)
        with patch("services.campaign_followups._claim", side_effect=concurrent_change):
            result = run_due_followups(dry_run=False)
        self.assertEqual(result["blocked"], 1)
        self.assertIsNone(self.row.sending_started_at)
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_cross_profile_message_id_collision_fails_closed(self, fetch, send):
        other = SenderProfile(name="Elektro", config_key="ELEKTRO", sender_name="Elektro",
                              sender_email="elektro@example.com", enabled=True)
        db.session.add(other)
        db.session.flush()
        db.session.add(EmailReply(sender_profile_id=other.id, imap_message_id="<reply@example.com>",
                                  from_email="other@example.com"))
        db.session.commit()
        fetch.return_value = [self.message()]
        self.assertEqual(run_due_followups(dry_run=False)["blocked"], 1)
        self.assertEqual(self.row.status, "scheduled")
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_stale_website_check_cancels_followup(self, fetch, send):
        self.campaign.require_no_website = True
        db.session.add(CompanyWebsiteCheck(company=self.row.campaign_recipient.company,
                                           status="not_found", checked_at=self.now - timedelta(days=31)))
        db.session.commit()
        self.assertEqual(run_due_followups(dry_run=False)["cancelled"], 1)
        fetch.assert_not_called()
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages", return_value=[])
    def test_lost_lock_after_inbox_scan_prevents_send(self, fetch, send):
        with patch("services.campaign_delivery.refresh_campaign_delivery_lock", return_value=False):
            self.assertEqual(run_due_followups(dry_run=False)["blocked"], 1)
        self.assertEqual(self.row.status, "scheduled")
        fetch.assert_called_once()
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_suppression_added_during_scan_prevents_send(self, fetch, send):
        def inbox(*args, **kwargs):
            db.session.add(Suppression(scope="email", value="kontakt@example.com"))
            db.session.commit()
            return []
        fetch.side_effect = inbox
        self.assertEqual(run_due_followups(dry_run=False)["suppressed"], 1)
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages", return_value=[])
    def test_permanently_failing_post_smtp_database_keeps_sending_claim(self, fetch, send):
        original_commit = db.session.commit
        def commit():
            if send.called:
                raise RuntimeError("simulated persistent SQL outage")
            return original_commit()
        with patch.object(db.session, "commit", side_effect=commit):
            result = run_due_followups(dry_run=False)
        db.session.expire_all()
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(self.row.status, "sending")
        self.assertIsNotNone(self.row.message_id)
        self.assertEqual(run_due_followups(dry_run=False)["selected"], 0)
        send.assert_called_once()

    @patch("services.campaign_followups.send_profile_message", side_effect=TimeoutError())
    @patch("services.campaign_followups.fetch_profile_messages", return_value=[])
    def test_uncertain_attempt_also_consumes_daily_cap(self, fetch, send):
        self.add_due_followup("Beta", "100002", "beta@example.com")
        self.campaign.daily_limit = 1
        db.session.commit()
        result = run_due_followups(dry_run=False)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(result["blocked"], 1)
        send.assert_called_once()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_cli_defaults_to_dry_run(self, fetch, send):
        result = self.app.test_cli_runner().invoke(args=["run-followups"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('"dry_run": true', result.output)
        self.assertIn('"eligible": 1', result.output)
        fetch.assert_not_called()
        send.assert_not_called()

    @patch("services.campaign_followups.send_profile_message")
    @patch("services.campaign_followups.fetch_profile_messages")
    def test_dry_run_reserves_preview_capacity_only(self, fetch, send):
        self.add_due_followup("Beta", "100002", "beta@example.com")
        self.campaign.daily_limit = 1
        db.session.commit()
        result = run_due_followups()
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(CampaignFollowUp.query.filter_by(status="scheduled").count(), 2)
        fetch.assert_not_called()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
