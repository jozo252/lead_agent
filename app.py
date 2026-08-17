from flask import Flask
import os
from dotenv import load_dotenv

from extensions import db, migrate, csrf, mail
from commands.rpo import (
    backfill_rpo_fields_command,
    enrich_contacts_command,
    enrich_websites_command,
    sync_rpo_command,
)
from commands.email import test_imap_command, test_smtp_command
from services.schema_migrations import (
    ensure_company_columns,
    ensure_email_reply_columns,
    ensure_lead_columns,
    ensure_sync_state_columns,
)


def create_app():
    load_dotenv()

    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-this")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///leads.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAIL_SERVER"] = os.environ.get("MAIL_SERVER")
    app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", 587))
    app.config["MAIL_USE_TLS"] = os.environ.get("MAIL_USE_TLS", "true").lower() == "true"
    app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME")
    app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD")
    app.config["MAIL_DEFAULT_SENDER"] = os.environ.get("MAIL_DEFAULT_SENDER")
    app.config["GOOGLE_PLACES_API_KEY"] = os.environ.get("GOOGLE_PLACES_API_KEY")
    app.config["IMAP_SERVER"] = os.environ.get("IMAP_SERVER")
    app.config["IMAP_USERNAME"] = os.environ.get("IMAP_USERNAME")
    app.config["IMAP_PASSWORD"] = os.environ.get("IMAP_PASSWORD")
    app.config["BRAVE_API_KEY"] = os.environ.get("BRAVE_API_KEY")
    app.config["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY")
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    mail.init_app(app)
    app.cli.add_command(sync_rpo_command)
    app.cli.add_command(enrich_contacts_command)
    app.cli.add_command(backfill_rpo_fields_command)
    app.cli.add_command(enrich_websites_command)
    app.cli.add_command(test_smtp_command)
    app.cli.add_command(test_imap_command)
    from routes import main_bp
    app.register_blueprint(main_bp)

    with app.app_context():
        import models
        db.create_all()
        ensure_company_columns()
        ensure_sync_state_columns()
        ensure_lead_columns()
        ensure_email_reply_columns()

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
