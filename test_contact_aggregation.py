from types import SimpleNamespace
import unittest

from services.rpo_sync import (
    aggregate_company_contacts,
    build_derived_website_url,
    is_directory_domain,
    score_search_result,
    select_best_company_contacts,
)


class AggregateCompanyContactsTests(unittest.TestCase):
    def setUp(self):
        self.company = SimpleNamespace(
            ico="48061999",
            official_name="Elektroinštalácie Poprad, s.r.o.",
            municipality="Poprad",
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
        self.assertTrue(
            is_directory_domain(
                "https://www.ifirmy.sk/firma/019636-optimetall-sro"
            )
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
