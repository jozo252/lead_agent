import unittest

from services.rpo_sync import (
    extract_source_register_name,
    matches_target_sole_trader_focus,
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


class RpoSoleTraderTargetFocusTests(unittest.TestCase):
    def test_accepts_main_construction_nace(self):
        record = {
            "statisticalCodes": {
                "mainActivity": {"code": "43.21"},
            },
        }

        self.assertTrue(matches_target_sole_trader_focus(record))

    def test_accepts_site_preparation_nace(self):
        record = {
            "statisticalCodes": {
                "mainActivity": {"code": "43.12"},
            },
        }

        self.assertTrue(matches_target_sole_trader_focus(record))

    def test_accepts_building_finishing_nace(self):
        record = {
            "statisticalCodes": {
                "mainActivity": {"code": "43.35"},
            },
        }

        self.assertTrue(matches_target_sole_trader_focus(record))

    def test_rejects_unselected_specialized_construction_nace(self):
        record = {
            "statisticalCodes": {
                "mainActivity": {"code": "43.99"},
            },
        }

        self.assertFalse(matches_target_sole_trader_focus(record))

    def test_accepts_related_technical_nace(self):
        record = {
            "statisticalCodes": {
                "mainActivity": {"code": "71.12"},
            },
        }

        self.assertTrue(matches_target_sole_trader_focus(record))

    def test_accepts_current_electrical_secondary_activity(self):
        record = {
            "statisticalCodes": {
                "mainActivity": {"code": "0240"},
            },
            "activities": [
                {
                    "economicActivityDescription": (
                        "Inštalácia elektrických rozvodov a zariadení"
                    ),
                },
            ],
        }

        self.assertTrue(matches_target_sole_trader_focus(record))

    def test_accepts_current_industrial_automation_activity(self):
        record = {
            "statisticalCodes": {
                "mainActivity": {"code": "6201"},
            },
            "activities": [
                {
                    "economicActivityDescription": (
                        "Projektovanie priemyselnej automatizácie"
                    ),
                },
            ],
        }

        self.assertTrue(matches_target_sole_trader_focus(record))

    def test_rejects_automated_data_processing_activity(self):
        record = {
            "statisticalCodes": {
                "mainActivity": {"code": "6209"},
            },
            "activities": [
                {
                    "economicActivityDescription": (
                        "Automatizované spracovanie dát"
                    ),
                },
            ],
        }

        self.assertFalse(matches_target_sole_trader_focus(record))

    def test_rejects_electric_arc_welding_activity(self):
        record = {
            "statisticalCodes": {
                "mainActivity": {"code": "2562"},
            },
            "activities": [
                {
                    "economicActivityDescription": (
                        "Vykonávanie zváracích prác elektrickým oblúkom"
                    ),
                },
            ],
        }

        self.assertFalse(matches_target_sole_trader_focus(record))

    def test_rejects_electronic_data_consulting_activity(self):
        record = {
            "statisticalCodes": {
                "mainActivity": {"code": "6202"},
            },
            "activities": [
                {
                    "economicActivityDescription": (
                        "Poradenské služby ohľadne elektronických "
                        "zariadení na spracovanie dát"
                    ),
                },
            ],
        }

        self.assertFalse(matches_target_sole_trader_focus(record))

    def test_rejects_electronics_installation_without_electrical_work(self):
        record = {
            "statisticalCodes": {
                "mainActivity": {"code": "4712"},
            },
            "activities": [
                {
                    "economicActivityDescription": (
                        "Montáž spotrebnej elektroniky "
                        "(bez zásahu do elektroinštalácie)"
                    ),
                },
            ],
        }

        self.assertFalse(matches_target_sole_trader_focus(record))

    def test_rejects_rental_of_electrical_machines(self):
        record = {
            "statisticalCodes": {
                "mainActivity": {"code": "7739"},
            },
            "activities": [
                {
                    "economicActivityDescription": (
                        "Prenájom elektrických strojov a zariadení"
                    ),
                },
            ],
        }

        self.assertFalse(matches_target_sole_trader_focus(record))

    def test_accepts_repairs_of_electrical_machines(self):
        record = {
            "statisticalCodes": {
                "mainActivity": {"code": "9522"},
            },
            "activities": [
                {
                    "economicActivityDescription": (
                        "Opravy a údržba elektrických strojov a prístrojov"
                    ),
                },
            ],
        }

        self.assertTrue(matches_target_sole_trader_focus(record))

    def test_ignores_expired_electrical_activity(self):
        record = {
            "statisticalCodes": {
                "mainActivity": {"code": "5611"},
            },
            "activities": [
                {
                    "economicActivityDescription": "Elektroinštalácie",
                    "validTo": "2025-01-01",
                },
            ],
        }

        self.assertFalse(matches_target_sole_trader_focus(record))

    def test_rejects_unrelated_trade(self):
        record = {
            "statisticalCodes": {
                "mainActivity": {"code": "5611"},
            },
            "activities": [
                {
                    "economicActivityDescription": "Reštauračné služby",
                },
            ],
        }

        self.assertFalse(matches_target_sole_trader_focus(record))
