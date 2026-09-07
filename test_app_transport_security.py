import unittest

from flask import jsonify, request

from app import create_app
from extensions import db
from models import Campaign, LandingPage
from services.landing_pages import default_landing_page_content


class AppTransportSecurityTests(unittest.TestCase):
    def build_app(self, **overrides):
        config = {
            "TESTING": True,
            "APP_ENV": "development",
            "SECRET_KEY": "test-secret",
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "WTF_CSRF_ENABLED": False,
            "TRUSTED_HOSTS": None,
            "INTERNAL_PROXY_TOKEN": "proxy-test-token-at-least-32-characters",
        }
        config.update(overrides)
        app = create_app(config)

        @app.route("/_security-probe", methods=["GET", "POST"])
        def security_probe():
            if request.method == "POST":
                request.get_data()
            return jsonify(
                host=request.host,
                remote_addr=request.remote_addr,
                scheme=request.scheme,
            )

        return app

    def test_internal_responses_receive_safe_headers_without_breaking_inline_assets(self):
        app = self.build_app()
        response = app.test_client().get("/_security-probe")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Referrer-Policy"], "same-origin")
        self.assertEqual(
            response.headers["Permissions-Policy"],
            "camera=(), microphone=(), geolocation=()",
        )
        self.assertEqual(
            response.headers["Content-Security-Policy"],
            "frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("Strict-Transport-Security", response.headers)

    def test_production_trusts_one_proxy_hop_and_emits_hsts_for_https(self):
        app = self.build_app(
            APP_ENV="production",
            SECRET_KEY="strong-production-test-secret-at-least-32",
            TRUSTED_HOSTS=["leadagent.gallax.io"],
        )
        response = app.test_client().get(
            "/_security-probe",
            base_url="http://leadagent.gallax.io",
            headers={
                "X-Forwarded-For": "198.51.100.24",
                "X-Forwarded-Proto": "https",
                "X-LeadAgent-Proxy-Token": "proxy-test-token-at-least-32-characters",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["remote_addr"], "198.51.100.24")
        self.assertEqual(response.json["scheme"], "https")
        self.assertEqual(
            response.headers["Strict-Transport-Security"],
            "max-age=31536000",
        )
        self.assertEqual(app.config["PREFERRED_URL_SCHEME"], "https")

    def test_production_rejects_untrusted_host_and_keeps_hsts_off_plain_http(self):
        app = self.build_app(
            APP_ENV="production",
            SECRET_KEY="strong-production-test-secret-at-least-32",
            TRUSTED_HOSTS=["leadagent.gallax.io"],
        )
        client = app.test_client()

        rejected = client.get(
            "/_security-probe",
            base_url="https://attacker.example",
            headers={
                "X-LeadAgent-Proxy-Token": "proxy-test-token-at-least-32-characters",
            },
        )
        plain_http = client.get(
            "/_security-probe",
            base_url="http://leadagent.gallax.io",
            headers={
                "X-LeadAgent-Proxy-Token": "proxy-test-token-at-least-32-characters",
            },
        )

        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(plain_http.status_code, 200)
        self.assertNotIn("Strict-Transport-Security", plain_http.headers)

    def test_production_internal_route_requires_reverse_proxy_token(self):
        app = self.build_app(
            APP_ENV="production",
            SECRET_KEY="strong-production-test-secret-at-least-32",
            TRUSTED_HOSTS=["leadagent.gallax.io"],
        )
        client = app.test_client()

        missing = client.get(
            "/_security-probe",
            base_url="https://leadagent.gallax.io",
        )
        wrong = client.get(
            "/_security-probe",
            base_url="https://leadagent.gallax.io",
            headers={"X-LeadAgent-Proxy-Token": "wrong"},
        )
        allowed = client.get(
            "/_security-probe",
            base_url="https://leadagent.gallax.io",
            headers={
                "X-LeadAgent-Proxy-Token": "proxy-test-token-at-least-32-characters",
            },
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(allowed.status_code, 200)

    def test_production_public_paths_do_not_require_reverse_proxy_token(self):
        app = self.build_app(
            APP_ENV="production",
            SECRET_KEY="strong-production-test-secret-at-least-32",
            TRUSTED_HOSTS=["leadagent.gallax.io"],
            WORK_API_TOKEN="work-token",
        )
        client = app.test_client()

        with app.app_context():
            db.create_all()
            landing = client.get(
                "/ponuka/does-not-exist",
                base_url="https://leadagent.gallax.io",
            )
            work_api = client.get(
                "/integrations/work/campaigns/999/batch",
                base_url="https://leadagent.gallax.io",
            )
            db.session.remove()
            db.drop_all()

        self.assertEqual(landing.status_code, 404)
        self.assertEqual(work_api.status_code, 401)

    def test_default_production_hosts_include_only_the_public_name(self):
        app = self.build_app(
            APP_ENV="production",
            SECRET_KEY="strong-production-test-secret-at-least-32",
            TRUSTED_HOSTS=None,
        )

        self.assertEqual(
            app.config["TRUSTED_HOSTS"],
            ["leadagent.gallax.io"],
        )

    def test_comma_separated_trusted_hosts_are_normalized(self):
        app = self.build_app(
            APP_ENV="production",
            SECRET_KEY="strong-production-test-secret-at-least-32",
            TRUSTED_HOSTS="leadagent.gallax.io, localhost",
        )

        self.assertEqual(
            app.config["TRUSTED_HOSTS"],
            ["leadagent.gallax.io", "localhost"],
        )

    def test_request_body_limit_returns_413_and_keeps_security_headers(self):
        app = self.build_app(MAX_CONTENT_LENGTH=64)
        response = app.test_client().post(
            "/_security-probe",
            data=b"x" * 65,
            content_type="application/octet-stream",
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")


class PublicLandingHeaderTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            {
                "TESTING": True,
                "APP_ENV": "development",
                "SECRET_KEY": "test-secret",
                "SQLALCHEMY_DATABASE_URI": "sqlite://",
                "WTF_CSRF_ENABLED": False,
                "TRUSTED_HOSTS": None,
                "LANDING_OPERATOR_NAME": "Test Operator",
                "LANDING_OPERATOR_ADDRESS": "Testovacia 1, Bratislava",
                "LANDING_OPERATOR_ICO": "12345678",
                "LANDING_OPERATOR_PHONE": "+421 900 000 000",
                "LANDING_OPERATOR_REGISTER": "Test register",
            }
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        campaign = Campaign(
            name="Security headers",
            offer_type="product",
            offer_stage="ready",
            offer_description="Test offer",
            subject_template="Test subject",
            body_template="Test body",
        )
        db.session.add(campaign)
        db.session.flush()
        self.page = LandingPage(
            campaign=campaign,
            slug="security-headers",
            preview_token="preview-token",
            status="published",
            contact_email="contact@example.test",
            content=default_landing_page_content(campaign),
        )
        db.session.add(self.page)
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_published_landing_keeps_route_specific_policy_and_is_cacheable(self):
        response = self.client.get("/ponuka/security-headers")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers["Content-Security-Policy"].startswith(
                "default-src 'none'"
            )
        )
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertNotIn("Cache-Control", response.headers)

    def test_preview_landing_keeps_private_no_store_policy(self):
        response = self.client.get(
            "/ponuka/security-headers?preview=preview-token"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store, private")
        self.assertEqual(response.headers["X-Robots-Tag"], "noindex, nofollow")


if __name__ == "__main__":
    unittest.main()
