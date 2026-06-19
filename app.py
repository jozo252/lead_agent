from flask import Flask
import os
from dotenv import load_dotenv

from extensions import db, migrate, csrf, mail


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

    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    mail.init_app(app)
    from routes import main_bp
    app.register_blueprint(main_bp)

    with app.app_context():
        import models
        db.create_all()

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)