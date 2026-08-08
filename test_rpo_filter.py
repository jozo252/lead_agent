import unittest

from services.rpo_sync import should_skip_rpo_record


class RpoLegalFormFilterTests(unittest.TestCase):
    def test_accepts_limited_liability_company(self):
        normalized = {
            "ico": "12345678",
            "official_name": "Príklad, s.r.o.",
            "legal_form": "Spoločnosť s ručením obmedzeným",
        }

        self.assertFalse(should_skip_rpo_record(normalized))

    def test_rejects_self_employed_professional(self):
        normalized = {
            "ico": "51727846",
            "official_name": "Ing. Patrik Vlček",
            "legal_form": (
                "Slobodné povolanie-fyzická osoba podnikajúca "
                "na základe iného ako živnostenského zákona"
            ),
        }

        self.assertTrue(
            should_skip_rpo_record(normalized, "Zoznam znalcov")
        )
