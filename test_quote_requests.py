import unittest
from unittest.mock import patch

from app import create_app
from extensions import db
from models import EmailReply, Lead, OutboundEmail, QuoteRequest, Suppression
from services.quote_requests import (
    _claim_ready_quote,
    create_quote_request,
    prepare_quote,
    send_prepared_quote,
)


class QuoteRequestTests(unittest.TestCase):
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

        self.lead = Lead(company_name="Test Stavby", email="info@test-stavby.sk")
        self.reply = EmailReply(
            lead=self.lead,
            from_email="zakaznik@example.test",
            subject="Prosba o cenu",
            text_body="Koľko by stálo 20 hodín práce?",
        )
        db.session.add_all([self.lead, self.reply])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def create_and_prepare(self):
        quote, _ = create_quote_request(self.lead, self.reply)
        return prepare_quote(
            quote,
            amount="38.50",
            currency="eur",
            unit="hodina",
            vat_text="bez DPH",
            terms="Doprava 0,35 EUR/km.",
            validity_days="14",
            sender_signature="Testovací dodávateľ",
        )

    def test_reply_creates_only_one_waiting_request(self):
        first, created = create_quote_request(self.lead, self.reply)
        second, created_again = create_quote_request(self.lead, self.reply)

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.status, "awaiting_price")
        self.assertEqual(first.recipient_email, "zakaznik@example.test")
        self.assertEqual(QuoteRequest.query.count(), 1)

    def test_price_is_validated_and_rendered_deterministically(self):
        quote = self.create_and_prepare()

        self.assertEqual(quote.status, "ready")
        self.assertEqual(str(quote.amount), "38.50")
        self.assertEqual(quote.currency, "EUR")
        self.assertIn("38,50 EUR / hodina", quote.body)
        self.assertIn("DPH: bez DPH", quote.body)
        self.assertNotIn("Koľko by stálo", quote.body)
        self.assertIsNotNone(quote.approval_token)

    @patch("services.quote_requests.mail.send")
    def test_explicit_authorization_sends_exactly_once(self, send):
        quote = self.create_and_prepare()
        token = quote.approval_token

        with self.assertRaises(PermissionError):
            send_prepared_quote(quote.id, token)
        self.assertEqual(db.session.get(QuoteRequest, quote.id).status, "ready")

        sent = send_prepared_quote(quote.id, token, authorized=True)
        with self.assertRaises(ValueError):
            send_prepared_quote(quote.id, token, authorized=True)

        self.assertEqual(sent.status, "sent")
        self.assertEqual(OutboundEmail.query.count(), 1)
        send.assert_called_once()

    def test_claim_is_atomic(self):
        quote = self.create_and_prepare()
        token = quote.approval_token

        first = _claim_ready_quote(quote.id, token)
        second = _claim_ready_quote(quote.id, token)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(db.session.get(QuoteRequest, quote.id).status, "sending")

    @patch("services.quote_requests.mail.send")
    def test_suppression_blocks_quote_before_claim(self, send):
        quote = self.create_and_prepare()
        db.session.add(
            Suppression(
                scope="email",
                value="zakaznik@example.test",
                reason="Neželá si správy",
            )
        )
        db.session.commit()

        with self.assertRaises(ValueError):
            send_prepared_quote(
                quote.id,
                quote.approval_token,
                authorized=True,
            )

        self.assertEqual(db.session.get(QuoteRequest, quote.id).status, "suppressed")
        send.assert_not_called()

    @patch("services.quote_requests.mail.send")
    def test_lead_page_creates_prices_and_sends_quote(self, send):
        client = self.app.test_client()
        response = client.post(
            f"/lead/{self.lead.id}/quote-request",
            data={"reply_id": self.reply.id},
            follow_redirects=True,
        )
        quote = QuoteRequest.query.one()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Cenové požiadavky".encode("utf-8"), response.data)
        self.assertIn("awaiting_price".encode(), response.data)

        response = client.post(
            f"/lead/{self.lead.id}/quote-request/{quote.id}/send",
            data={
                "amount": "1200",
                "currency": "EUR",
                "unit": "celkom",
                "vat_text": "bez DPH",
                "terms": "Rozsah podľa obhliadky.",
                "validity_days": "14",
                "sender_signature": "Testovací dodávateľ",
                "action": "send",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.session.get(QuoteRequest, quote.id).status, "sent")
        self.assertEqual(OutboundEmail.query.count(), 1)
        send.assert_called_once()

    @patch("services.quote_requests.mail.send", side_effect=TimeoutError("timeout"))
    def test_uncertain_mail_failure_is_not_retryable_automatically(self, send):
        quote = self.create_and_prepare()

        with self.assertRaises(TimeoutError):
            send_prepared_quote(
                quote.id,
                quote.approval_token,
                authorized=True,
            )

        stored = db.session.get(QuoteRequest, quote.id)
        self.assertEqual(stored.status, "unknown")
        self.assertIn("skontroluj poštu", stored.last_error)
        with self.assertRaises(ValueError):
            send_prepared_quote(
                quote.id,
                quote.approval_token,
                authorized=True,
            )
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
