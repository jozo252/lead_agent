from types import SimpleNamespace
import unittest

from services.rpo_sync import (
    aggregate_company_contacts,
    build_derived_website_url,
    build_derived_website_urls,
    is_directory_domain,
    is_foreign_country_domain,
    score_search_result,
    select_best_company_contacts,
    strip_legal_suffix,
)


class AggregateCompanyContactsTests(unittest.TestCase):
    def setUp(self):
        self.company = SimpleNamespace(
            ico="48061999",
            official_name="Elektroinštalácie Poprad, s.r.o.",
            municipality="Poprad",
        )

    def test_derives_domain_after_full_sro_suffix(self):
        company = SimpleNamespace(
            official_name="ANTES GM, spol. s r.o.",
        )

        self.assertEqual(strip_legal_suffix(company.official_name), "ANTES GM")
        self.assertEqual(
            build_derived_website_url(company),
            "https://antesgm.sk",
        )

    def test_includes_legal_form_domain_variant(self):
        company = SimpleNamespace(official_name="AJAP s.r.o.")

        self.assertEqual(
            build_derived_website_urls(company),
            ["https://ajap.sk", "https://ajapsro.sk"],
        )

    def test_includes_local_variant_without_slovakia_in_name(self):
        company = SimpleNamespace(official_name="DATS Slovakia, s.r.o.")

        self.assertEqual(
            build_derived_website_urls(company),
            [
                "https://datsslovakia.sk",
                "https://datsslovakiasro.sk",
                "https://dats.sk",
            ],
        )

    def test_excludes_foreign_country_domains_but_keeps_generic_domains(self):
        self.assertTrue(is_foreign_country_domain("https://ajap.pt"))
        self.assertTrue(is_foreign_country_domain("https://example.de"))
        self.assertFalse(is_foreign_country_domain("https://ajapsro.sk"))
        self.assertFalse(is_foreign_country_domain("https://example.com"))

    def test_ico_verified_email_domain_becomes_company_website(self):
        company = SimpleNamespace(
            ico="36294781",
            official_name="ANTES GM, spol. s r.o.",
            municipality="Trenčín",
        )
        results = [
            {
                "url": "https://www.infoma.sk/firma/4214",
                "title": "ANTES GM, spol. s r.o.",
                "description": (
                    "IČO: 36294781, e-mail: antesgm@antesgm.sk, "
                    "telefón: 032 658 25 23"
                ),
            }
        ]

        aggregated = aggregate_company_contacts(company, results)

        self.assertEqual(
            aggregated["websites"][0]["value"],
            "https://antesgm.sk",
        )
        self.assertEqual(
            aggregated["websites"][0]["reason"],
            "ico_verified_email_domain",
        )

    def test_public_email_domain_does_not_become_company_website(self):
        results = [
            {
                "url": "https://register.example/remeselnik/48061999",
                "title": "Elektroinštalácie Poprad, s.r.o.",
                "description": (
                    "Poprad, IČO: 48061999, "
                    "elektroinstalaciepoprad@gmail.com"
                ),
            },
        ]

        aggregated = aggregate_company_contacts(self.company, results)

        self.assertEqual(aggregated["websites"], [])
        self.assertNotIn(
            "https://gmail.com",
            [item["value"] for item in aggregated["websites"]],
        )

    def test_catalog_domain_is_not_treated_as_company_website(self):
        results = [
            {
                "url": "https://www.zoznam.sk/firma/3172905",
                "title": "Elektroinštalácie Poprad",
                "description": (
                    "IČO: 48061999. Web: https://elektroinstalaciepoprad.sk "
                    "Email: elektroinstalaciepoprad@gmail.com"
                ),
            },
            {
                "url": "https://elektroinstalaciepoprad.sk/kontakt",
                "title": "Elektroinštalácie Poprad | Kontakt",
                "description": (
                    "Poprad, IČO: 48061999, "
                    "info@elektroinstalaciepoprad.sk, 0903 628 912"
                ),
                "website_validated": True,
            },
            {
                "url": "https://unrelated.example/contact",
                "title": "Unrelated business",
                "description": "sales@unrelated.example, 0948 132 867",
            },
        ]

        aggregated = aggregate_company_contacts(self.company, results)

        self.assertEqual(
            aggregated["verified_domains"],
            ["elektroinstalaciepoprad.sk"],
        )
        self.assertEqual(
            [item["value"] for item in aggregated["emails"]],
            [
                "info@elektroinstalaciepoprad.sk",
            ],
        )
        self.assertEqual(
            [item["value"] for item in aggregated["phones"]],
            ["+421903628912"],
        )
        self.assertGreaterEqual(aggregated["emails"][0]["confidence"], 85)
        self.assertNotIn("zoznam.sk", aggregated["verified_domains"])

    def test_selects_one_contact_per_type_above_threshold(self):
        aggregated = {
            "websites": [
                {
                    "value": "https://example.sk",
                    "confidence": 70,
                    "reason": "verified_domain_match",
                },
            ],
            "emails": [
                {
                    "value": "info@example.sk",
                    "confidence": 80,
                    "reason": "verified_source_domain",
                },
                {
                    "value": "owner@example.sk",
                    "confidence": 90,
                    "reason": "ico_match",
                },
            ],
            "phones": [
                {
                    "value": "+421903628912",
                    "confidence": 70,
                    "reason": "verified_domain_match",
                },
            ],
        }

        selected = select_best_company_contacts(aggregated)

        self.assertEqual(selected["website"]["value"], "https://example.sk")
        self.assertEqual(selected["email"]["value"], "owner@example.sk")
        self.assertNotIn("phone", selected)

    def test_directory_result_is_never_selected_for_saving(self):
        results = [
            {
                "url": "https://www.edb.eu/sk/firma/nowire/kontakty",
                "title": "Nowire s.r.o.",
                "description": "IČO: 48061999, +421 903 628 912",
            },
        ]

        aggregated = aggregate_company_contacts(self.company, results)
        selected = select_best_company_contacts(aggregated)

        self.assertEqual(aggregated["websites"], [])
        self.assertEqual(aggregated["phones"], [])
        self.assertEqual(selected, {})

    def test_known_business_directory_is_blocked(self):
        directory_urls = [
            "https://www.ifirmy.sk/firma/019636-optimetall-sro",
            "https://dodavatelia.123dopyt.sk/1734227-dusan-gazur-elektro-plyn",
            "https://www.zlateruky.sk/remeselnik/12806-jozef-pecenadsky",
            "http://dusan-gazur-elektro-plyn.trade.sk/",
            "https://www.daibau.sk/zhotovitel/example",
            "https://www.cylex.sk/example.html",
            "https://www.industrycontact.sk/detail/example",
            "https://www.aaadopyt.sk/dodavatelia/38",
        ]

        for url in directory_urls:
            with self.subTest(url=url):
                self.assertTrue(is_directory_domain(url))

    def test_content_validated_directory_stays_unverified_candidate(self):
        results = [
            {
                "url": "https://www.zlateruky.sk/remeselnik/12806-example",
                "title": "Elektroinštalácie Poprad, s.r.o.",
                "description": (
                    "Poprad, IČO: 48061999, "
                    "elektroinstalaciepoprad@gmail.com, 0903 628 912"
                ),
                "website_validated": True,
                "website_ico_validated": True,
            },
        ]

        aggregated = aggregate_company_contacts(self.company, results)
        selected = select_best_company_contacts(
            aggregated,
            include_candidates=True,
        )

        self.assertEqual(aggregated["websites"], [])
        self.assertNotIn("website", selected)
        self.assertEqual(
            selected["email"]["reason"],
            "ico_match_unverified_source",
        )
        self.assertEqual(
            selected["phone"]["reason"],
            "ico_match_unverified_source",
        )

    def test_ico_matched_directory_contacts_are_saved_as_candidates(self):
        results = [
            {
                "url": "https://www.zoznam.sk/firma/3172905",
                "title": "Elektroinštalácie Poprad",
                "description": (
                    "Poprad, IČO: 48061999, "
                    "elektroinstalaciepoprad@gmail.com, 0903 628 912"
                ),
            },
        ]

        aggregated = aggregate_company_contacts(self.company, results)
        selected = select_best_company_contacts(
            aggregated,
            include_candidates=True,
        )

        self.assertEqual(aggregated["emails"], [])
        self.assertEqual(
            aggregated["possible_contacts"]["emails"][0]["value"],
            "elektroinstalaciepoprad@gmail.com",
        )
        self.assertEqual(
            aggregated["possible_contacts"]["phones"][0]["value"],
            "+421903628912",
        )
        self.assertEqual(
            selected["email"]["reason"],
            "ico_match_unverified_source",
        )
        self.assertEqual(
            selected["phone"]["reason"],
            "ico_match_unverified_source",
        )

    def test_name_and_location_match_is_saved_as_candidate(self):
        results = [
            {
                "url": "https://example.org/elektroinstalacie-poprad",
                "title": "Elektroinštalácie Poprad",
                "description": "Poprad, info@elektroinstalaciepoprad.sk, 0903 628 912",
            },
        ]

        aggregated = aggregate_company_contacts(self.company, results)
        selected = select_best_company_contacts(
            aggregated,
            include_candidates=True,
        )

        self.assertEqual(
            selected["email"]["reason"],
            "name_location_unverified_source",
        )
        self.assertEqual(
            selected["phone"]["reason"],
            "name_location_unverified_source",
        )

    def test_social_profile_contacts_are_possible_but_not_verified(self):
        company = SimpleNamespace(
            ico="47649151",
            official_name="RAVUS s. r. o.",
            municipality="Nová Dedinka",
        )
        results = [
            {
                "url": "https://www.facebook.com/Ravus-1688667401384681/",
                "title": "Ravus | Nová Dedinka | Facebook",
                "description": (
                    "Nová Dedinka, Slovakia, +421 905 714 336, "
                    "ravussro@gmail.com"
                ),
            },
        ]

        aggregated = aggregate_company_contacts(company, results)
        selected = select_best_company_contacts(aggregated)

        self.assertEqual(aggregated["emails"], [])
        self.assertEqual(aggregated["phones"], [])
        self.assertEqual(
            aggregated["possible_contacts"]["emails"][0]["value"],
            "ravussro@gmail.com",
        )
        self.assertEqual(
            aggregated["possible_contacts"]["phones"][0]["value"],
            "+421905714336",
        )
        self.assertEqual(selected, {})

    def test_scores_verified_company_signals(self):
        company = SimpleNamespace(
            ico="47649151",
            official_name="RAVUS s. r. o.",
            municipality="Nová Dedinka",
        )
        results = [
            {
                "url": "https://ravus.sk/kontakt",
                "title": "RAVUS | Nová Dedinka",
                "description": (
                    "IČO: 47649151, info@ravus.sk, +421 905 714 336"
                ),
                "website_validated": True,
            },
        ]

        aggregated = aggregate_company_contacts(company, results)

        evidence = aggregated["evidence"][0]
        self.assertEqual(evidence["source_score"], 100)
        self.assertIn("ico_match:+50", evidence["score_breakdown"])
        self.assertIn("company_name_match:+25", evidence["score_breakdown"])
        self.assertIn(
            "email_domain_matches_website:+15",
            evidence["score_breakdown"],
        )

    def test_matches_company_name_in_derived_domain(self):
        company = SimpleNamespace(
            ico="35975750",
            official_name="NOWIRE s.r.o.",
            municipality=None,
        )
        result = {
            "title": "Úvodná stránka",
            "url": "https://nowire.sk",
            "description": "",
        }

        self.assertEqual(build_derived_website_url(company), "https://nowire.sk")
        self.assertEqual(score_search_result(company, result), 25)


if __name__ == "__main__":
    unittest.main()
