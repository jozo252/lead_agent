from flask import Flask
import os
from dotenv import load_dotenv

from extensions import db, migrate, csrf, mail
from commands.rpo import (
    backfill_rpo_fields_command,
    enrich_contacts_command,
    enrich_financials_command,
    enrich_websites_command,
    sync_rpo_command,
)
from commands.email import test_imap_command, test_smtp_command
from commands.locations import import_postal_locations_command
from commands.campaigns import run_campaigns_command


DEFAULT_DEVELOPMENT_SECRET_KEY = "dev-secret-key-change-this"
PRODUCTION_ENVIRONMENTS = {"prod", "production"}


def _validate_secret_key_config(app):
    """Reject a missing or well-known development secret in production."""
    environment = str(app.config.get("APP_ENV") or "").strip().casefold()
    secret_key = app.config.get("SECRET_KEY")

    if environment in PRODUCTION_ENVIRONMENTS:
        if not secret_key or secret_key == DEFAULT_DEVELOPMENT_SECRET_KEY:
            raise RuntimeError(
                "Production requires a non-default SECRET_KEY. "
                "Set a strong, unique SECRET_KEY environment variable."
            )
    elif not secret_key:
        app.config["SECRET_KEY"] = DEFAULT_DEVELOPMENT_SECRET_KEY

    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = (
        app.config.get("SESSION_COOKIE_SAMESITE") or "Lax"
    )
    if environment in PRODUCTION_ENVIRONMENTS:
        app.config["SESSION_COOKIE_SECURE"] = True


def create_app(config=None):
    load_dotenv()

    app = Flask(__name__)

    app.config["APP_ENV"] = os.environ.get(
        "APP_ENV",
        os.environ.get("FLASK_ENV", "development"),
    )
    app.config["SECRET_KEY"] = (
        os.environ.get("SECRET_KEY") or DEFAULT_DEVELOPMENT_SECRET_KEY
    )
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL",
        "sqlite:///leads.db",
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAIL_SERVER"] = os.environ.get("MAIL_SERVER")
    app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", 587))
    app.config["MAIL_USE_TLS"] = os.environ.get("MAIL_USE_TLS", "true").lower() == "true"
    app.config["MAIL_USE_SSL"] = os.environ.get("MAIL_USE_SSL", "false").lower() == "true"
    app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME")
    app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD")
    app.config["MAIL_DEFAULT_SENDER"] = os.environ.get("MAIL_DEFAULT_SENDER")
    app.config["GOOGLE_PLACES_API_KEY"] = os.environ.get("GOOGLE_PLACES_API_KEY")
    app.config["IMAP_SERVER"] = os.environ.get("IMAP_SERVER")
    app.config["IMAP_USERNAME"] = os.environ.get("IMAP_USERNAME")
    app.config["IMAP_PASSWORD"] = os.environ.get("IMAP_PASSWORD")
    app.config["BRAVE_API_KEY"] = os.environ.get("BRAVE_API_KEY")
    app.config["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY")
    app.config["OPENAI_MODEL"] = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
    app.config["WORK_API_TOKEN"] = os.environ.get("WORK_API_TOKEN")
    app.config["HUBSPOT_ACCESS_TOKEN"] = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    app.config["HUBSPOT_API_BASE"] = os.environ.get(
        "HUBSPOT_API_BASE",
        "https://api.hubapi.com",
    ).rstrip("/")
    app.config["PUBLIC_BASE_URL"] = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    app.config["LANDING_OPERATOR_NAME"] = os.environ.get("LANDING_OPERATOR_NAME")
    app.config["LANDING_OPERATOR_ADDRESS"] = os.environ.get(
        "LANDING_OPERATOR_ADDRESS"
    )
    app.config["LANDING_OPERATOR_ICO"] = os.environ.get("LANDING_OPERATOR_ICO")
    app.config["LANDING_OPERATOR_PHONE"] = os.environ.get("LANDING_OPERATOR_PHONE")
    app.config["LANDING_OPERATOR_REGISTER"] = os.environ.get(
        "LANDING_OPERATOR_REGISTER"
    )
    if config:
        app.config.update(config)
    _validate_secret_key_config(app)

    db.init_app(app)
    migrate.init_app(app, db, compare_type=True, render_as_batch=True)
    csrf.init_app(app)
    mail.init_app(app)
    app.cli.add_command(sync_rpo_command)
    app.cli.add_command(enrich_contacts_command)
    app.cli.add_command(enrich_financials_command)
    app.cli.add_command(backfill_rpo_fields_command)
    app.cli.add_command(enrich_websites_command)
    app.cli.add_command(test_smtp_command)
    app.cli.add_command(test_imap_command)
    app.cli.add_command(import_postal_locations_command)
    app.cli.add_command(run_campaigns_command)
    from routes import main_bp
    app.register_blueprint(main_bp)
    from campaign_routes import campaign_bp
    app.register_blueprint(campaign_bp)
    from landing_page_routes import landing_page_bp
    app.register_blueprint(landing_page_bp)
    from integration_routes import integration_bp
    app.register_blueprint(integration_bp)

    import models  # noqa: F401

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
