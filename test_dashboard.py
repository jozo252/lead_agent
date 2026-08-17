import os
import unittest
from datetime import date, datetime, timedelta

from flask import Flask

from extensions import db
from models import Company, CompanyContact, EmailReply, Lead, OutboundEmail

os.environ.setdefault("OPENAI_API_KEY", "test-dashboard-key")

from routes import LEAD_STATUSES, dashboard_metrics, dashboard_work_queues, main_bp


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="test-secret",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        cls.app.register_blueprint(main_bp)

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_dashboard_uses_unique_stored_records(self):
        company = Company(official_name="Firma s e-mailom")
        second_company = Company(official_name="Firma bez e-mailu")
        db.session.add_all([company, second_company])
        db.session.flush()
        db.session.add_all([
            CompanyContact(company=company, contact_type="email", value="info@example.test", source_type="test"),
            CompanyContact(company=company, contact_type="EMAIL", value="obchod@example.test", source_type="test"),
            CompanyContact(company=second_company, contact_type="phone", value="+421900000000", source_type="test"),
        ])
        today = date.today()
        first_lead = Lead(company_name="Prvý lead", next_follow_up_at=datetime.combine(today, datetime.min.time()))
        second_lead = Lead(company_name="Druhý lead", next_follow_up_at=datetime.combine(today - timedelta(days=1), datetime.min.time()))
        third_lead = Lead(company_name="Tretí lead")
        db.session.add_all([first_lead, second_lead, third_lead])
        db.session.flush()
        db.session.add_all([
            OutboundEmail(lead=first_lead, message_id="first@example.test", recipient="first@example.test", subject="Prvé oslovenie", body="Text"),
            OutboundEmail(lead=first_lead, message_id="follow-up@example.test", recipient="first@example.test", subject="Follow-up", body="Text"),
            OutboundEmail(lead=second_lead, message_id="second@example.test", recipient="second@example.test", subject="Druhé oslovenie", body="Text"),
            EmailReply(lead=first_lead, subject="Prvá odpoveď"),
            EmailReply(lead=first_lead, subject="Druhá odpoveď"),
            EmailReply(lead=third_lead, subject="Importovaná odpoveď"),
        ])
        db.session.commit()

        self.assertEqual(dashboard_metrics(), {
            "companies": 2,
            "companies_with_email": 1,
            "contacts": 3,
            "leads": 3,
            "contacted_leads": 2,
            "replies": 2,
            "open_follow_ups": 2,
            "reply_rate": 50.0,
        })
        response = self.app.test_client().get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-metric="reply-rate">50.0 %', response.data)

    def test_dashboard_excludes_terminal_and_future_follow_ups(self):
        today = date.today()
        db.session.add_all([
            Lead(company_name="Splatný lead", next_follow_up_at=datetime.combine(today, datetime.min.time())),
            Lead(company_name="Ukončený lead", status=LEAD_STATUSES[-3], next_follow_up_at=datetime.combine(today - timedelta(days=1), datetime.min.time())),
            Lead(company_name="Budúci lead", next_follow_up_at=datetime.combine(today + timedelta(days=1), datetime.min.time())),
        ])
        db.session.commit()

        self.assertEqual(dashboard_metrics()["open_follow_ups"], 1)

    def test_dashboard_reports_zero_rate_without_outreach(self):
        lead = Lead(company_name="Lead bez odoslania")
        db.session.add(lead)
        db.session.flush()
        db.session.add(EmailReply(lead=lead, subject="Odpoveď bez oslovenia"))
        db.session.commit()

        self.assertEqual(dashboard_metrics()["replies"], 1)
        self.assertEqual(dashboard_metrics()["reply_rate"], 0)

    def test_work_queues_include_only_actionable_records(self):
        today = date.today()
        due_lead = Lead(
            company_name="Splatný follow-up",
            next_follow_up_at=datetime.combine(
                today - timedelta(days=1),
                datetime.min.time(),
            ),
        )
        closed_lead = Lead(
            company_name="Uzatvorený follow-up",
            status=LEAD_STATUSES[-3],
            next_follow_up_at=datetime.combine(
                today - timedelta(days=1),
                datetime.min.time(),
            ),
        )
        waiting_lead = Lead(
            company_name="Čaká na odpoveď",
            status="Oslovený",
            last_contacted_at=datetime.combine(
                today - timedelta(days=6),
                datetime.min.time(),
            ),
        )
        replied_lead = Lead(
            company_name="Nová prijatá odpoveď",
            status="Oslovený",
            last_contacted_at=datetime.combine(
                today - timedelta(days=6),
                datetime.min.time(),
            ),
        )
        answered_lead = Lead(
            company_name="Už odpovedal",
            status="Oslovený",
            last_contacted_at=datetime.combine(
                today - timedelta(days=6),
                datetime.min.time(),
            ),
        )
        db.session.add_all([
            due_lead,
            closed_lead,
            waiting_lead,
            replied_lead,
            answered_lead,
        ])
        db.session.flush()
        unanswered_reply = EmailReply(
            lead=replied_lead,
            subject="Potrebujem odpoveď",
        )
        answered_reply = EmailReply(
            lead=answered_lead,
            subject="Už vybavené",
            reply_sent_at=datetime.utcnow(),
        )
        db.session.add_all([unanswered_reply, answered_reply])
        db.session.commit()

        queues = dashboard_work_queues()

        self.assertEqual([lead.id for lead in queues["due_follow_ups"]], [due_lead.id])
        self.assertEqual([lead.id for lead in queues["waiting_for_reply"]], [waiting_lead.id])
        self.assertEqual([reply.id for reply in queues["unanswered_replies"]], [unanswered_reply.id])


if __name__ == "__main__":
    unittest.main()
