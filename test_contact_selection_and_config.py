import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import DEFAULT_DEVELOPMENT_SECRET_KEY, create_app
from services.contact_selection import (
    normalized_contact_email,
    select_email_contact,
)


def contact(
    value,
    *,
    verified=False,
    primary=False,
    confidence=0,
    contact_type="email",
):
    return SimpleNamespace(
        value=value,
        is_verified=verified,
        is_primary=primary,
        confidence_score=confidence,
        contact_type=contact_type,
    )


class ContactSelectionTests(unittest.TestCase):
    def test_verified_contact_has_priority_over_unverified_primary(self):
        unverified_primary = contact(
            "primary@example.com",
            primary=True,
            confidence=100,
        )
        verified_secondary = contact(
            "verified@example.com",
            verified=True,
            confidence=10,
        )

        selected = select_email_contact(
            [unverified_primary, verified_secondary]
        )

        self.assertIs(selected, verified_secondary)

    def test_primary_then_confidence_break_ties_between_verified_contacts(self):
        high_confidence = contact(
            "confidence@example.com",
            verified=True,
            confidence=100,
        )
        primary = contact(
            "primary@example.com",
            verified=True,
            primary=True,
            confidence=10,
        )
        higher_primary_confidence = contact(
            "best@example.com",
            verified=True,
            primary=True,
            confidence=90,
        )

        selected = select_email_contact(
            [high_confidence, primary, higher_primary_confidence]
        )

        self.assertIs(selected, higher_primary_confidence)

    def test_invalid_and_non_email_contacts_are_ignored(self):
        invalid = contact("not-an-email", verified=True, primary=True)
        phone = contact(
            "+421900000000",
            verified=True,
            primary=True,
            contact_type="phone",
        )
        valid = contact("  USER@example.com ", confidence=50)

        self.assertIs(select_email_contact([invalid, phone, valid]), valid)
        self.assertEqual(normalized_contact_email(valid), "USER@example.com")

    def test_automatic_selection_can_require_verified_email(self):
        unverified = contact("valid@example.com", primary=True, confidence=100)

        self.assertIs(select_email_contact([unverified]), unverified)
        self.assertIsNone(
            select_email_contact([unverified], require_verified=True)
        )


class ProductionSecretConfigTests(unittest.TestCase):
    def app_config(self, **overrides):
        config = {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "APP_ENV": "development",
            "INTERNAL_PROXY_TOKEN": "proxy-test-token-at-least-32-characters",
        }
        config.update(overrides)
        return config

    def test_production_rejects_missing_secret(self):
        with self.assertRaisesRegex(RuntimeError, "non-default SECRET_KEY"):
            create_app(self.app_config(APP_ENV="production", SECRET_KEY=None))

    def test_production_rejects_default_secret(self):
        with self.assertRaisesRegex(RuntimeError, "non-default SECRET_KEY"):
            create_app(
                self.app_config(
                    APP_ENV="prod",
                    SECRET_KEY=DEFAULT_DEVELOPMENT_SECRET_KEY,
                )
            )

    def test_production_rejects_short_secret(self):
        with self.assertRaisesRegex(RuntimeError, "at least 32"):
            create_app(
                self.app_config(
                    APP_ENV="production",
                    SECRET_KEY="too-short",
                )
            )

    def test_production_accepts_explicit_unique_secret(self):
        app = create_app(
            self.app_config(
                APP_ENV="production",
                SECRET_KEY="a-unique-production-secret-for-this-test",
            )
        )

        self.assertEqual(
            app.config["SECRET_KEY"],
            "a-unique-production-secret-for-this-test",
        )
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
        self.assertTrue(app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")

    def test_production_rejects_missing_internal_proxy_token(self):
        with self.assertRaisesRegex(RuntimeError, "INTERNAL_PROXY_TOKEN"):
            create_app(
                self.app_config(
                    APP_ENV="production",
                    SECRET_KEY="a-unique-production-secret-for-this-test",
                    INTERNAL_PROXY_TOKEN=None,
                )
            )

    def test_unknown_environment_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "APP_ENV"):
            create_app(
                self.app_config(
                    APP_ENV="produciton",
                    SECRET_KEY="a-unique-production-secret-for-this-test",
                )
            )

    def test_local_environment_keeps_development_fallback(self):
        app = create_app(self.app_config(SECRET_KEY=None))

        self.assertEqual(app.config["SECRET_KEY"], DEFAULT_DEVELOPMENT_SECRET_KEY)

    def test_landing_operator_settings_are_loaded_from_environment(self):
        values = {
            "LANDING_OPERATOR_NAME": "Gallax s. r. o.",
            "LANDING_OPERATOR_ADDRESS": "Testovacia 1, Bratislava",
            "LANDING_OPERATOR_ICO": "12345678",
            "LANDING_OPERATOR_PHONE": "+421900000000",
            "LANDING_OPERATOR_REGISTER": "OR MS Bratislava III",
        }
        with patch.dict(os.environ, values, clear=False):
            app = create_app(self.app_config(SECRET_KEY="test-secret"))

        for key, value in values.items():
            self.assertEqual(app.config[key], value)

    def test_openai_model_is_loaded_from_environment(self):
        with patch.dict(os.environ, {"OPENAI_MODEL": "gpt-5-mini"}, clear=False):
            app = create_app(self.app_config(SECRET_KEY="test-secret"))

        self.assertEqual(app.config["OPENAI_MODEL"], "gpt-5-mini")


if __name__ == "__main__":
    unittest.main()
