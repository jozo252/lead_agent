import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask

from extensions import db
from models import Campaign, CampaignFollowUp, CampaignRecipient, Company, CompanyContact, Lead, OutboundEmail, Suppression
from services.salon_discovery import discover_salon_contacts, parse_salon_listing, recheck_salon_contact


URL = "https://www.notino.sk/salony/salon-jp/"
OWN_URL = "https://salon-jp.sk/kontakt/"


def page(*, url=URL, name="Salón JP", city="Poprad", email="kontakt@salon-jp.sk",
         extra="", booking=None, address=None, extra_entity=None):
    data = {
        "@context": "https://schema.org", "@type": "HairSalon", "name": name,
        "url": url, "email": email,
        "address": address or {"@type": "PostalAddress", "streetAddress": "Hlavná 1",
                               "addressLocality": city, "postalCode": "058 01", "addressCountry": "SK"},
    }
    if booking is None:
        booking = "Rezervovať telefonicky. Tieto služby nemôžete rezervovať online."
    data = [data, extra_entity] if extra_entity else data
    return ('<html><head><title>' + name + '</title><script type="application/ld+json">'
            + json.dumps(data) + '</script></head><body><main><h1>' + name + '</h1>'
            + city + ' Hlavná 1 <a href="mailto:' + email + '">' + email + '</a><p>'
            + booking + '</p>' + extra + '</main><footer><a href="mailto:info@notino.sk">'
            'info@notino.sk</a></footer></body></html>')


class SalonParserTests(unittest.TestCase):
    def parse(self, html=None, url=URL, locations=None):
        return parse_salon_listing(html or page(), url, locations or ["Poprad", "Svit", "Kežmarok"])

    def test_listing_requires_exact_business_contact_and_ignores_platform_footer(self):
        result = self.parse()
        self.assertEqual(result["email"], "kontakt@salon-jp.sk")
        self.assertEqual(result["booking_signal"], "telephone_only_on_listing")
        self.assertEqual(result["name_kind"], "public_business_display_name")
        self.assertIsNone(result["website_url"])

    def test_rejects_platform_email_even_when_in_business_schema(self):
        self.assertIsNone(self.parse(page(email="info@notino.sk")))

    def test_rejects_footer_only_email(self):
        html = page().replace('<a href="mailto:kontakt@salon-jp.sk">kontakt@salon-jp.sk</a>', "")
        self.assertIsNone(self.parse(html))

    def test_rejects_ambiguous_emails(self):
        self.assertIsNone(self.parse(page(extra='<a href="mailto:other@salon-jp.sk">Other</a>')))

    def test_rejects_out_of_area_and_similar_municipality(self):
        for city in ["Bratislava", "Poprad okolie", "Svitavy"]:
            with self.subTest(city=city):
                self.assertIsNone(self.parse(page(city=city)))

    def test_rejects_category_path_or_another_listing_identity(self):
        self.assertIsNone(self.parse(url="https://www.notino.sk/salony/"))
        self.assertIsNone(self.parse(page(url="https://www.notino.sk/salony/other/")))

    def test_booking_absence_without_positive_phone_signal_is_not_evidence(self):
        self.assertIsNone(self.parse(page(booking="Kontaktujte nás.")))

    def test_rejects_positive_online_booking_or_service_booking_provider(self):
        for extra in ["Rezervovať online", '<a href="https://bookio.com/salon/1">Objednať</a>',
                      '<iframe src="https://widget.reservio.com/1"></iframe>',
                      '<script src="https://fresha.com/widget.js"></script>',
                      '<input type="date">', '<a href="/rezervacia/">Rezervácia</a>']:
            with self.subTest(extra=extra):
                self.assertIsNone(self.parse(page(extra=extra)))

    def test_rejects_ambiguous_schema_entities(self):
        second = {"@type": "BeautySalon", "name": "Another salon"}
        self.assertIsNone(self.parse(page(extra_entity=second)))

    def test_own_site_must_match_its_schema_host(self):
        self.assertIsNone(self.parse(page(), url=OWN_URL))

    def test_own_site_requires_explicit_appointment_context(self):
        self.assertIsNone(self.parse(page(url=OWN_URL, booking="Telefón: 0900 123 456"), url=OWN_URL))
        result = self.parse(page(url=OWN_URL, booking="Na objednanie nás kontaktujte telefonicky alebo emailom."), url=OWN_URL)
        self.assertEqual(result["booking_signal"], "telephone_or_email_appointment_request")
        self.assertEqual(result["website_url"], OWN_URL)

    def test_own_mail_request_form_is_not_live_calendar(self):
        address = {"streetAddress": "Starý trh 482/40, 060 01 Kežmarok", "addressCountry": "SK"}
        result = self.parse(page(url=OWN_URL, city="Kežmarok", address=address,
                                booking="Odoslaním sa otvorí váš e-mailový klient.",
                                extra='<a href="#contact">Rezervovať termín</a>'
                                      '<a href="https://www.facebook.com/profile.php?id=salonm">Facebook</a>'), url=OWN_URL)
        self.assertEqual(result["municipality"], "Kežmarok")
        self.assertEqual(result["street"], "Starý trh 482/40")
        self.assertEqual(result["booking_signal"], "email_appointment_request")


class SalonDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True)
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.campaign = Campaign(name="Salóny", offer_type="service", offer_description="Rezervácie",
                                 subject_template="Ponuka", body_template="Ukážka",
                                 targeting_profile={"salon_discovery": True, "location_keywords": ["Poprad"]})
        db.session.add(self.campaign)
        db.session.commit()
        self.search = Mock(return_value={"web": {"results": [{"url": URL}]}})
        self.fetch = Mock(return_value=SimpleNamespace(status_code=200, url=URL, text=page()))

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def run_discovery(self, **kwargs):
        return discover_salon_contacts(self.campaign, search=self.search, fetch=self.fetch, **kwargs)

    def test_default_dry_run_has_no_database_or_orm_changes(self):
        result = self.run_discovery()
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(Company.query.count(), 0)
        self.assertIsNone(self.campaign.last_run_summary)
        self.assertFalse(db.session.new or db.session.dirty or db.session.deleted)

    def test_explicit_import_is_idempotent_does_not_send_or_create_recipients(self):
        first = self.run_discovery(dry_run=False)
        db.session.commit()
        second = self.run_discovery(dry_run=False)
        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(self.fetch.call_count, 1)
        company = Company.query.one()
        self.assertEqual(company.official_name, "Salón JP")
        self.assertIsNone(company.ico)
        self.assertIsNone(company.legal_form)
        contact = CompanyContact.query.one()
        self.assertEqual(contact.source_type, "salon_public_listing")
        self.assertTrue(contact.is_verified)
        self.assertIsNotNone(contact.last_verified_at)
        for model in [CampaignRecipient, CampaignFollowUp, OutboundEmail]:
            self.assertEqual(model.query.count(), 0)

    def test_own_website_is_retained_and_never_recorded_as_absent(self):
        self.search.return_value = {"web": {"results": [{"url": OWN_URL}]}}
        self.fetch.return_value = SimpleNamespace(status_code=200, url=OWN_URL,
                                                  text=page(url=OWN_URL, booking="Objednanie telefonicky."))
        self.assertEqual(self.run_discovery(dry_run=False)["imported"], 1)
        self.assertEqual(CompanyContact.query.filter_by(contact_type="website").one().value, OWN_URL)
        self.assertIsNone(Company.query.one().website_check)

    def test_existing_email_in_other_company_blocks_import(self):
        company = Company(official_name="Existing legal operator")
        company.contacts.append(CompanyContact(contact_type="email", value=" KONTAKT@SALON-JP.SK ",
                                               source_type="manual", is_verified=False))
        db.session.add(company)
        db.session.commit()
        self.assertEqual(self.run_discovery(dry_run=False)["imported"], 0)
        self.assertEqual(Company.query.count(), 1)

    def test_existing_business_name_and_city_blocks_duplicate_company(self):
        db.session.add(Company(official_name="Salón JP", municipality="Poprad"))
        db.session.commit()
        self.assertEqual(self.run_discovery(dry_run=False)["imported"], 0)

    def test_global_email_domain_suppression_blocks_new_company(self):
        for scope, value in [("email", "kontakt@salon-jp.sk"), ("domain", "salon-jp.sk")]:
            with self.subTest(scope=scope):
                record = Suppression(scope=scope, value=value)
                db.session.add(record)
                db.session.commit()
                self.assertEqual(self.run_discovery(dry_run=False)["imported"], 0)
                db.session.delete(record)
                db.session.commit()

    def test_outbound_history_blocks_import_even_without_company_contact(self):
        lead = Lead(company_name="Old salon", email="old@salon-jp.sk")
        db.session.add(lead)
        db.session.flush()
        db.session.add(OutboundEmail(lead_id=lead.id, recipient="KONTAKT@SALON-JP.SK", message_id="old",
                                     subject="Old", body="Old"))
        db.session.commit()
        self.assertEqual(self.run_discovery(dry_run=False)["imported"], 0)

    def test_http_error_is_sanitized_and_never_imports(self):
        for response in [SimpleNamespace(status_code=403, url=URL, text=page()),
                         SimpleNamespace(status_code=200, url="https://other.sk/", text=page())]:
            self.fetch.return_value = response
            result = self.run_discovery(dry_run=False)
            self.assertEqual(result["imported"], 0)
            self.assertIn("source_unavailable", result["errors"])
        self.fetch.side_effect = RuntimeError("secret-token-that-must-not-be-reported")
        self.assertNotIn("secret-token", json.dumps(self.run_discovery()))

    def test_provider_search_failures_are_sanitized(self):
        self.search.side_effect = RuntimeError("secret-key")
        result = self.run_discovery()
        self.assertEqual(result["errors"], ["search_unavailable"])
        self.fetch.assert_not_called()

    def test_bounded_fetches_rotate_past_previously_checked_pages(self):
        urls = [f"https://www.notino.sk/salony/test-{i}/" for i in range(20)]
        self.search.return_value = {"web": {"results": [{"url": url} for url in urls]}}
        self.fetch.side_effect = lambda url, **kwargs: SimpleNamespace(status_code=403, url=url, text="")
        first = self.run_discovery(dry_run=False, limit=99)
        self.assertEqual(first["fetched"], 12)
        second = self.run_discovery(dry_run=False, limit=8)
        self.assertEqual(second["fetched"], 8)
        self.assertEqual([call.args[0] for call in self.fetch.call_args_list], urls)
        self.search.assert_called_with('"Poprad" salón kaderníctvo kozmetika kontakt email objednanie', count=20)

    def test_requires_explicit_campaign_flag_and_valid_locations(self):
        self.campaign.targeting_profile = {"location_keywords": ["Poprad"]}
        self.assertFalse(self.run_discovery(dry_run=False)["enabled"])
        self.search.assert_not_called()
        self.campaign.targeting_profile = {"salon_discovery": True, "location_keywords": "Poprad"}
        self.assertIn("invalid_locations", self.run_discovery()["errors"])
        self.search.assert_not_called()


class SalonRecheckTests(unittest.TestCase):
    def setUp(self):
        self.old_time = datetime.now() - timedelta(days=8)
        self.old_evidence = {"source_type": "salon_public_listing", "source_url": URL,
                             "checked_at": self.old_time.isoformat()}
        self.company = SimpleNamespace(official_name="Salón JP", municipality="Poprad",
                                       analysis_evidence=[self.old_evidence, {"other": "retained"}])
        self.contact = SimpleNamespace(company=self.company, source_type="salon_public_listing",
                                       contact_type="email", is_verified=True, source_url=URL,
                                       value="kontakt@salon-jp.sk", last_verified_at=self.old_time)
        self.fetch = Mock(return_value=SimpleNamespace(status_code=200, url=URL, text=page()))

    def recheck(self):
        return recheck_salon_contact(self.contact, ["Poprad", "Svit", "Kežmarok"], fetch=self.fetch)

    def assert_unchanged(self):
        self.assertEqual(self.contact.last_verified_at, self.old_time)
        self.assertTrue(self.contact.is_verified)
        self.assertEqual(self.company.analysis_evidence, [self.old_evidence, {"other": "retained"}])

    def test_success_refreshes_timestamp_and_replaces_only_source_evidence(self):
        self.company.official_name = "SALON JP"  # display spelling/diacritics normalize
        self.assertTrue(self.recheck())
        self.assertGreater(self.contact.last_verified_at, self.old_time)
        self.assertEqual(len(self.company.analysis_evidence), 2)
        self.assertIn({"other": "retained"}, self.company.analysis_evidence)
        refreshed = self.company.analysis_evidence[-1]
        self.assertEqual(refreshed["email"], "kontakt@salon-jp.sk")
        self.assertEqual(refreshed["booking_signal"], "telephone_only_on_listing")

    def test_changed_email_name_or_city_is_held_without_modifying_old_verification(self):
        for html in [page(email="new@salon-jp.sk"), page(name="Different Salon"), page(city="Svit")]:
            with self.subTest(html=html[:70]):
                self.fetch.return_value.text = html
                self.assertFalse(self.recheck())
                self.assert_unchanged()

    def test_changed_source_or_unreachable_source_does_not_refresh(self):
        for response in [SimpleNamespace(status_code=403, url=URL, text=page()),
                         SimpleNamespace(status_code=200, url="https://other.sk/", text=page()),
                         SimpleNamespace(status_code=200, url="https://www.notino.sk/salony/other/", text=page())]:
            with self.subTest(url=response.url, status=response.status_code):
                self.fetch.return_value = response
                self.assertFalse(self.recheck())
                self.assert_unchanged()
        self.fetch.side_effect = RuntimeError("provider-secret")
        self.assertFalse(self.recheck())
        self.assert_unchanged()

    def test_new_online_booking_holds_followup(self):
        self.fetch.return_value.text = page(extra='<a href="https://bookio.com/salon/1">Rezervovať</a>')
        self.assertFalse(self.recheck())
        self.assert_unchanged()

    def test_unrelated_source_or_unverified_contact_never_fetches(self):
        self.contact.source_type = "manual"
        self.assertFalse(self.recheck())
        self.contact.source_type = "salon_public_listing"
        self.contact.is_verified = False
        self.assertFalse(self.recheck())
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
