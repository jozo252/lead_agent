import unittest

from services.rpo_sync import (
    extract_source_register_name,
    normalized_rpo_fields,
    should_skip_rpo_record,
)


class RpoLegalFormFilterTests(unittest.TestCase):
    def test_accepts_limited_liability_company(self):
        normalized = {
            "ico": "12345678",
            "official_name": "Príklad, s.r.o.",
            "legal_form": "Spoločnosť s ručením obmedzeným",
        }

        self.assertFalse(should_skip_rpo_record(normalized))

    def test_accepts_sole_trader_from_trade_register(self):
        record = {
            "id": 19067171,
            "data": {
                "fullNames": [{"value": "Ján Goroľ"}],
                "identifiers": [{"value": "57483540"}],
                "legalForms": [
                    {
                        "value": {
                            "code": "101",
                            "value": (
                                "Podnikateľ-fyzická osoba-nezapísaný "
                                "v obchodnom registri"
                            ),
                        }
                    }
                ],
                "sourceRegister": {
                    "value": {
                        "code": "2",
                        "value": "Živnostenský register",
                    }
                },
            },
        }
        normalized = normalized_rpo_fields(record)
        source_register = extract_source_register_name(record)

        self.assertFalse(
            should_skip_rpo_record(
                normalized,
                source_register,
            )
        )
        self.assertEqual(source_register, "Živnostenský register")

    def test_rejects_non_trade_self_employed_person(self):
        normalized = {
            "ico": "51727846",
            "official_name": "Ing. Patrik Vlček",
            "legal_form": (
                "Podnikateľ-fyzická osoba-nezapísaný "
                "v obchodnom registri"
            ),
        }

        self.assertTrue(
            should_skip_rpo_record(normalized, "Iný register")
        )

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

    def test_extracts_main_sk_nace_activity(self):
        record = {
            "data": {
                "fullNames": [{"value": "Test s.r.o."}],
                "identifiers": [{"value": "12345678"}],
                "statisticalCodes": {
                    "mainActivity": {
                        "code": "4321",
                        "value": "Elektroinštalačné práce",
                    },
                },
            },
        }

        normalized = normalized_rpo_fields(record)

        self.assertEqual(normalized["sk_nace_code"], "4321")
        self.assertEqual(
            normalized["sk_nace_name"],
            "Elektroinštalačné práce",
        )
