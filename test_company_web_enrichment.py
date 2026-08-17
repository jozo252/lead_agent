import unittest

from services.company_web_enrichment import normalize_company_analysis


class CompanyWebEnrichmentTests(unittest.TestCase):
    def test_keeps_missing_data_empty(self):
        analysis = normalize_company_analysis({
            "company_type": None,
            "services": [],
            "markets": None,
            "works_abroad": "unknown",
            "regions": [],
            "employee_count": 0,
            "employee_count_source": "neoverené",
            "subcontractor_need": "unknown",
            "outreach_relevant": "yes",
            "analysis_reason": "",
            "analysis_evidence": [],
        })

        self.assertIsNone(analysis["company_type"])
        self.assertIsNone(analysis["services"])
        self.assertIsNone(analysis["works_abroad"])
        self.assertIsNone(analysis["employee_count"])
        self.assertIsNone(analysis["employee_count_source"])
        self.assertIsNone(analysis["subcontractor_need"])
        self.assertIsNone(analysis["analysis_reason"])
        self.assertIsNone(analysis["analysis_evidence"])

    def test_normalizes_only_supported_values(self):
        analysis = normalize_company_analysis({
            "company_type": " Elektroinštalačná firma ",
            "services": ["elektroinštalácie", "elektroinštalácie", "montáže"],
            "markets": ["priemysel"],
            "works_abroad": True,
            "regions": ["Slovensko", "Nemecko"],
            "employee_count": 25,
            "employee_count_source": "Na webe uvádzajú tím 25 ľudí.",
            "subcontractor_need": "high",
            "outreach_relevant": True,
            "analysis_reason": "Hľadajú elektrikárov.",
            "analysis_evidence": [
                {
                    "quote": "Hľadáme elektrikárov.",
                    "source_url": "firma.sk/kariera",
                },
            ],
        })

        self.assertEqual(analysis["company_type"], "Elektroinštalačná firma")
        self.assertEqual(analysis["services"], ["elektroinštalácie", "montáže"])
        self.assertEqual(analysis["employee_count"], 25)
        self.assertEqual(analysis["subcontractor_need"], "high")
        self.assertEqual(
            analysis["analysis_evidence"][0]["source_url"],
            "https://firma.sk/kariera",
        )
