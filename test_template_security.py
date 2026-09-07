import unittest
from html.parser import HTMLParser

from app import create_app
from extensions import db
from models import Company, CompanyContact, EmailReply, Lead, QuoteRequest


class OnclickCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = []

    def handle_starttag(self, _tag, attrs):
        for name, value in attrs:
            if name == "onclick" and value is not None:
                self.values.append(value)


class HrefCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value is not None:
                self.values.append(value)


class TemplateSecurityTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "WTF_CSRF_ENABLED": False,
            "MAIL_SUPPRESS_SEND": True,
        })
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    @staticmethod
    def onclick_values(response):
        parser = OnclickCollector()
        parser.feed(response.get_data(as_text=True))
        return parser.values

    def test_untrusted_email_is_not_interpolated_into_javascript_handlers(self):
        payload = "x'-alert`1`-'@example.com"
        lead = Lead(
            company_name="Bezpečnostný test",
            email=payload,
            suggested_message="Koncept správy",
        )
        reply = EmailReply(lead=lead, from_email=payload, subject="Žiadosť o cenu")
        quote = QuoteRequest(
            lead=lead,
            email_reply=reply,
            recipient_email=payload,
            status="awaiting_price",
        )
        db.session.add_all([lead, reply, quote])
        db.session.commit()

        handlers = (
            self.onclick_values(self.client.get(f"/lead/{lead.id}"))
            + self.onclick_values(self.client.get("/"))
        )

        self.assertTrue(handlers)
        self.assertFalse(any(payload in handler for handler in handlers))
        self.assertTrue(any("Odoslať tento e-mail?" in handler for handler in handlers))
        self.assertTrue(any(
            "Uložiť túto cenu a odoslať ponuku?" in handler
            for handler in handlers
        ))

    def test_untrusted_website_scheme_is_never_rendered_as_a_link(self):
        lead = Lead(
            company_name="Bezpečnostný test URL",
            website="javascript:alert(document.domain)",
        )
        db.session.add(lead)
        db.session.commit()

        parser = HrefCollector()
        parser.feed(self.client.get(f"/lead/{lead.id}").get_data(as_text=True))
        parser.feed(self.client.get("/").get_data(as_text=True))

        self.assertFalse(any(value.casefold().startswith("javascript:") for value in parser.values))

    def test_new_lead_rejects_untrusted_website_scheme(self):
        response = self.client.post(
            "/",
            data={
                "company_name": "Bezpečnostný test URL",
                "website": "javascript:alert(document.domain)",
            },
            follow_redirects=True,
        )

        self.assertEqual(Lead.query.count(), 0)
        self.assertIn("HTTP alebo HTTPS", response.get_data(as_text=True))

    def test_untrusted_company_source_url_is_never_rendered_as_a_link(self):
        company = Company(ico="12345678", official_name="Bezpečnostný test")
        company.contacts.append(CompanyContact(
            contact_type="email",
            value="contact@example.com",
            source_type="brave_candidate",
            source_url="javascript://evil.example/%0Aalert(document.domain)",
        ))
        db.session.add(company)
        db.session.commit()

        parser = HrefCollector()
        parser.feed(
            self.client.get(f"/companies/{company.id}").get_data(as_text=True)
        )

        self.assertFalse(
            any(value.casefold().startswith("javascript:") for value in parser.values)
        )


if __name__ == "__main__":
    unittest.main()
