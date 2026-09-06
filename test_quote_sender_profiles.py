import unittest
from unittest.mock import patch

from flask import Flask

from extensions import db, mail
from models import Campaign, CampaignRecipient, Company, EmailReply, Lead, OutboundEmail, QuoteRequest, SenderProfile
from services.quote_requests import create_quote_request, prepare_quote, send_prepared_quote
from services.sender_profiles import SenderProfileError, resolve_reply_sender_profile, send_profile_message


class QuoteSenderProfileTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite://", MAIL_SUPPRESS_SEND=True,
            MAIL_DEFAULT_SENDER="legacy@example.test", SENDER_PROFILE_SETTINGS={},
        )
        db.init_app(self.app)
        mail.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.profile = self.add_profile("MG_STAV", "stav@example.test")
        self.other = self.add_profile("ELEKTRO", "elektro@example.test")
        company = Company(official_name="Test Stavby", ico="50000001")
        self.lead = Lead(company=company, company_name="Test Stavby", email="customer@example.test")
        self.campaign = Campaign(
            name="Stavby", offer_description="Stavebné práce", subject_template="Ponuka",
            body_template="Spolupráca", sender_profile=self.other,
        )
        self.recipient = CampaignRecipient(
            campaign=self.campaign, company=company, recipient_email=self.lead.email,
            subject="Ponuka", body="Spolupráca", status="sent",
        )
        self.reply = EmailReply(
            lead=self.lead, campaign_recipient=self.recipient, from_email=self.lead.email,
            subject="Cena?", text_body="Prosím cenovú ponuku.", imap_message_id="<incoming@example.test>",
        )
        db.session.add_all([self.lead, self.campaign, self.recipient, self.reply])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def add_profile(self, key, address):
        profile = SenderProfile(
            name=key, config_key=key, sender_name=key, sender_email=address,
            signature=f"Tím {key}", enabled=True,
        )
        self.app.config["SENDER_PROFILE_SETTINGS"][key] = {
            "MAIL_SERVER": "smtp.example.test", "MAIL_PORT": 587, "MAIL_USE_TLS": True,
            "MAIL_USERNAME": address, "MAIL_PASSWORD": "smtp-unit-test-secret",
        }
        db.session.add(profile)
        db.session.flush()
        return profile

    def outbound(self, profile, *, link_recipient=True):
        outbound = OutboundEmail(
            lead=self.lead, campaign_recipient=self.recipient if link_recipient else None,
            sender_profile=profile, recipient=self.lead.email, subject="Prvá ponuka",
            body="Spolupráca", message_id=f"<sent-{OutboundEmail.query.count()}@example.test>",
        )
        db.session.add(outbound)
        db.session.commit()
        return outbound

    def prepared(self):
        quote, _ = create_quote_request(self.lead, self.reply)
        return prepare_quote(
            quote, amount="100", currency="EUR", unit="celkom", vat_text="bez DPH",
            terms="Podľa dohody", validity_days=14, sender_signature="Zodpovedná osoba",
        )

    def test_durable_reply_profile_overrides_changed_campaign_and_sent_copy_matches(self):
        self.reply.sender_profile = self.profile
        db.session.commit()
        quote = self.prepared()
        with patch("services.quote_requests.mail.send") as legacy, patch(
            "services.quote_requests.send_profile_message", wraps=send_profile_message,
        ) as send:
            send_prepared_quote(quote.id, quote.approval_token, authorized=True)
        legacy.assert_not_called()
        send.assert_called_once()
        message, profile = send.call_args.args
        self.assertEqual(profile.id, self.profile.id)
        self.assertEqual(message.sender, ("MG_STAV", "stav@example.test"))
        self.assertEqual(message.extra_headers["In-Reply-To"], "<incoming@example.test>")
        self.assertEqual(message.extra_headers["References"], "<incoming@example.test>")
        outbound = OutboundEmail.query.one()
        self.assertEqual(outbound.sender_profile_id, self.profile.id)
        self.assertEqual(outbound.campaign_recipient_id, self.recipient.id)
        self.assertEqual(outbound.body, message.body)
        self.assertTrue(outbound.body.endswith(self.profile.signature))
        self.assertNotIn(self.other.signature, outbound.body)

    def test_unique_historical_account_is_used_instead_of_current_campaign_profile(self):
        self.outbound(self.profile)
        self.assertEqual(resolve_reply_sender_profile(self.reply).id, self.profile.id)
        quote = self.prepared()
        with patch("services.quote_requests.mail.send") as legacy, patch(
            "services.quote_requests.send_profile_message", wraps=send_profile_message,
        ) as send:
            send_prepared_quote(quote.id, quote.approval_token, authorized=True)
        self.assertEqual(send.call_args.args[1].id, self.profile.id)
        legacy.assert_not_called()

    def test_mixed_historical_accounts_fail_before_claim(self):
        self.outbound(self.profile)
        self.outbound(self.other)
        quote = self.prepared()
        token = quote.approval_token
        with patch("services.quote_requests.mail.send") as legacy, patch("services.quote_requests.send_profile_message") as send:
            with self.assertRaises(SenderProfileError):
                send_prepared_quote(quote.id, token, authorized=True)
        self.assertEqual(db.session.get(QuoteRequest, quote.id).status, "ready")
        self.assertIsNone(quote.sending_started_at)
        self.assertEqual(quote.approval_token, token)
        legacy.assert_not_called()
        send.assert_not_called()

    def test_legacy_and_profile_history_is_ambiguous(self):
        self.outbound(None)
        self.outbound(self.profile)
        with self.assertRaises(SenderProfileError):
            resolve_reply_sender_profile(self.reply)

    def test_dangling_explicit_reply_profile_is_not_treated_as_legacy(self):
        self.reply.sender_profile_id = 987654
        db.session.commit()
        quote = self.prepared()
        with patch("services.quote_requests.mail.send") as legacy, patch("services.quote_requests.send_profile_message") as send:
            with self.assertRaisesRegex(SenderProfileError, "už nie je dostupný"):
                send_prepared_quote(quote.id, quote.approval_token, authorized=True)
        self.assertEqual(quote.status, "ready")
        legacy.assert_not_called()
        send.assert_not_called()

    def test_disabled_or_incomplete_profile_preserves_price_approval(self):
        self.reply.sender_profile = self.profile
        self.profile.enabled = False
        db.session.commit()
        quote = self.prepared()
        with patch("services.quote_requests.send_profile_message") as send:
            with self.assertRaisesRegex(SenderProfileError, "vypnutý"):
                send_prepared_quote(quote.id, quote.approval_token, authorized=True)
        self.assertEqual(quote.status, "ready")
        self.assertIsNone(quote.sending_started_at)
        send.assert_not_called()

        self.profile.enabled = True
        self.app.config["SENDER_PROFILE_SETTINGS"][self.profile.config_key]["MAIL_PASSWORD"] = None
        db.session.commit()
        with self.assertRaisesRegex(SenderProfileError, "MAIL_PASSWORD"):
            send_prepared_quote(quote.id, quote.approval_token, authorized=True)
        self.assertEqual(quote.status, "ready")

    def test_legacy_history_stays_legacy_even_if_campaign_now_has_profile(self):
        self.outbound(None)
        quote = self.prepared()
        with patch("services.quote_requests.mail.send") as legacy, patch("services.quote_requests.send_profile_message") as send:
            send_prepared_quote(quote.id, quote.approval_token, authorized=True)
        legacy.assert_called_once()
        send.assert_not_called()
        self.assertIsNone(quote.outbound_email.sender_profile_id)

    def test_unlinked_reply_with_profile_history_requires_manual_identity_resolution(self):
        self.outbound(self.profile, link_recipient=False)
        self.reply.campaign_recipient = None
        db.session.commit()
        with self.assertRaisesRegex(SenderProfileError, "nemožno spoľahlivo určiť"):
            resolve_reply_sender_profile(self.reply)

    def test_missing_original_reply_with_profile_history_cannot_use_legacy_sender(self):
        self.outbound(self.profile)
        quote = self.prepared()
        quote.email_reply = None
        db.session.commit()
        with patch("services.quote_requests.mail.send") as legacy:
            with self.assertRaisesRegex(SenderProfileError, "Chýba pôvodná odpoveď"):
                send_prepared_quote(quote.id, quote.approval_token, authorized=True)
        self.assertEqual(quote.status, "ready")
        legacy.assert_not_called()

    def test_ambiguous_profile_transport_failure_is_unknown_and_not_retryable(self):
        self.reply.sender_profile = self.profile
        db.session.commit()
        quote = self.prepared()
        token = quote.approval_token
        with patch("services.quote_requests.send_profile_message", side_effect=SenderProfileError("SMTP výsledok nie je potvrdený.")) as send:
            with self.assertRaises(SenderProfileError):
                send_prepared_quote(quote.id, token, authorized=True)
            self.assertEqual(db.session.get(QuoteRequest, quote.id).status, "unknown")
            with self.assertRaises(ValueError):
                send_prepared_quote(quote.id, token, authorized=True)
        send.assert_called_once()
        self.assertEqual(OutboundEmail.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
