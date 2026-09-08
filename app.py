from flask import Flask, Response, request
import hmac
import os
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix
from services.safe_http import normalize_http_url

from extensions import db, migrate, csrf, mail
from commands.rpo import (
    backfill_rpo_fields_command,
    enrich_contacts_command,
    enrich_financials_command,
    enrich_websites_command,
    import_rpo_sole_traders_command,
    sync_rpo_command,
)
from commands.email import test_imap_command, test_smtp_command
from commands.locations import import_postal_locations_command
from commands.campaigns import run_campaigns_command
from commands.scouts import run_scouts_command
from commands.followups import run_followups_command


DEFAULT_DEVELOPMENT_SECRET_KEY = "dev-secret-key-change-this"
DEFAULT_MAX_CONTENT_LENGTH = 1024 * 1024
DEFAULT_PRODUCTION_TRUSTED_HOSTS = [
    "leadagent.gallax.io",
]
PRODUCTION_ENVIRONMENTS = {"prod", "production"}
KNOWN_ENVIRONMENTS = PRODUCTION_ENVIRONMENTS | {"dev", "development", "test", "testing"}
INTERNAL_PROXY_HEADER = "X-LeadAgent-Proxy-Token"
PUBLIC_PRODUCTION_PATH_PREFIXES = (
    "/ponuka/",
    "/integrations/work/",
)


def _validate_secret_key_config(app):
    """Reject a missing or well-known development secret in production."""
    environment = str(app.config.get("APP_ENV") or "").strip().casefold()
    secret_key = app.config.get("SECRET_KEY")

    if environment not in KNOWN_ENVIRONMENTS:
        raise RuntimeError(
            "APP_ENV must be one of: dev, development, test, testing, prod, production."
        )

    if environment in PRODUCTION_ENVIRONMENTS:
        if (
            not secret_key
            or secret_key == DEFAULT_DEVELOPMENT_SECRET_KEY
            or len(str(secret_key)) < 32
        ):
            raise RuntimeError(
                "Production requires a non-default SECRET_KEY with at least 32 characters. "
                "Set a strong, unique SECRET_KEY environment variable."
            )
        proxy_token = str(app.config.get("INTERNAL_PROXY_TOKEN") or "")
        if len(proxy_token) < 32:
            raise RuntimeError(
                "Production requires an INTERNAL_PROXY_TOKEN with at least 32 characters."
            )
    elif not secret_key:
        app.config["SECRET_KEY"] = DEFAULT_DEVELOPMENT_SECRET_KEY

    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = (
        app.config.get("SESSION_COOKIE_SAMESITE") or "Lax"
    )
    if environment in PRODUCTION_ENVIRONMENTS:
        app.config["SESSION_COOKIE_SECURE"] = True


def _parse_trusted_hosts(value):
    if not value:
        return None
    if isinstance(value, str):
        hosts = [host.strip() for host in value.split(",") if host.strip()]
        return hosts or None
    return list(value)


def _configure_request_security(app):
    """Trust the single loopback reverse proxy and constrain production hosts."""
    app.config["TRUSTED_HOSTS"] = _parse_trusted_hosts(
        app.config.get("TRUSTED_HOSTS")
    )
    environment = str(app.config.get("APP_ENV") or "").strip().casefold()
    if environment not in PRODUCTION_ENVIRONMENTS:
        return

    if not app.config.get("TRUSTED_HOSTS"):
        app.config["TRUSTED_HOSTS"] = list(DEFAULT_PRODUCTION_TRUSTED_HOSTS)
    app.config["PREFERRED_URL_SCHEME"] = "https"

    # Production Gunicorn listens only on loopback and has exactly one Nginx proxy.
    # Nginx overwrites X-Forwarded-Proto and appends the real client to
    # X-Forwarded-For, so only the final value from each header is trusted.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)


def _register_security_headers(app):
    environment = str(app.config.get("APP_ENV") or "").strip().casefold()
    is_production = environment in PRODUCTION_ENVIRONMENTS

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        # These directives block framing, injected base URLs and legacy object
        # embeds without disabling the existing inline styles and scripts.
        response.headers.setdefault(
            "Content-Security-Policy",
            "frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
        )
        if is_production and request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000",
            )

        # Published landing pages may be cached. Their route owns its stricter
        # public/preview cache and CSP policy; all internal and API responses
        # must remain out of browser and intermediary caches.
        if request.endpoint != "landing_pages.public_landing_page":
            response.headers["Cache-Control"] = "no-store"
        return response


def _register_internal_proxy_guard(app):
    """Keep internal CRM routes inaccessible through the loopback port."""
    environment = str(app.config.get("APP_ENV") or "").strip().casefold()
    if environment not in PRODUCTION_ENVIRONMENTS:
        return

    @app.before_request
    def require_internal_proxy_token():
        if request.path.startswith(PUBLIC_PRODUCTION_PATH_PREFIXES):
            return None
        configured = str(app.config.get("INTERNAL_PROXY_TOKEN") or "")
        supplied = request.headers.get(INTERNAL_PROXY_HEADER, "")
        if supplied and hmac.compare_digest(configured, supplied):
            return None
        return Response("Unauthorized\n", status=401, mimetype="text/plain")


def create_app(config=None):
    load_dotenv()

    app = Flask(__name__)

    app.config["APP_ENV"] = os.environ.get(
        "APP_ENV",
        os.environ.get("FLASK_ENV"),
    )
    app.config["SECRET_KEY"] = (
        os.environ.get("SECRET_KEY") or DEFAULT_DEVELOPMENT_SECRET_KEY
    )
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL",
        "sqlite:///leads.db",
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = int(
        os.environ.get("MAX_CONTENT_LENGTH", DEFAULT_MAX_CONTENT_LENGTH)
    )
    app.config["TRUSTED_HOSTS"] = _parse_trusted_hosts(
        os.environ.get("TRUSTED_HOSTS")
    )
    app.config["PREFERRED_URL_SCHEME"] = os.environ.get(
        "PREFERRED_URL_SCHEME",
        "http",
    )
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
    app.config["INTERNAL_PROXY_TOKEN"] = os.environ.get("INTERNAL_PROXY_TOKEN")
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
    if not app.config.get("APP_ENV") and app.config.get("TESTING") is True:
        app.config["APP_ENV"] = "testing"
    _validate_secret_key_config(app)
    _configure_request_security(app)
    _register_internal_proxy_guard(app)
    _register_security_headers(app)
    app.jinja_env.filters["safe_external_url"] = normalize_http_url

    db.init_app(app)
    migrate.init_app(app, db, compare_type=True, render_as_batch=True)
    csrf.init_app(app)
    mail.init_app(app)
    app.cli.add_command(sync_rpo_command)
    app.cli.add_command(import_rpo_sole_traders_command)
    app.cli.add_command(enrich_contacts_command)
    app.cli.add_command(enrich_financials_command)
    app.cli.add_command(backfill_rpo_fields_command)
    app.cli.add_command(enrich_websites_command)
    app.cli.add_command(test_smtp_command)
    app.cli.add_command(test_imap_command)
    app.cli.add_command(import_postal_locations_command)
    app.cli.add_command(run_campaigns_command)
    app.cli.add_command(run_scouts_command)
    app.cli.add_command(run_followups_command)
    from routes import main_bp
    app.register_blueprint(main_bp)
    from campaign_routes import campaign_bp
    app.register_blueprint(campaign_bp)
    from campaign_workflow_routes import workflow_bp
    app.register_blueprint(workflow_bp)
    from landing_page_routes import landing_page_bp
    app.register_blueprint(landing_page_bp)
    from integration_routes import integration_bp
    app.register_blueprint(integration_bp)

    import models  # noqa: F401

    return app


if __name__ == "__main__":
    create_app().run(debug=False)
