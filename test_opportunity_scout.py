import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from app import create_app
from extensions import db
from models import Campaign, Opportunity, ScoutRun
from services.opportunity_scout import normalize_source_url, run_campaign_scout


class OpportunityScoutTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "SQLALCHEMY_DATABASE_URI": "sqlite://",
                "WTF_CSRF_ENABLED": False,
            }
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.now = datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def add_campaign(self, *, enabled=True, status="active"):
        campaign = Campaign(
            name="Stavebné zákazky",
            offer_type="service",
            offer_description="Stavebné práce",
            subject_template="Ponuka spolupráce",
            body_template="Text",
            business_line="construction",
            status=status,
            daily_limit=5,
            batch_size=5,
            scout_enabled=enabled,
            scout_queries=["stavebné práce dopyt Poprad"],
            targeting_profile={
                "opportunity_keywords": ["stavebné práce", "Poprad"],
                "search_country": "SK",
                "search_language": "sk",
            },
        )
        db.session.add(campaign)
        db.session.commit()
        return campaign

    @staticmethod
    def payload(title="Hľadáme stavebnú firmu"):
        return {
            "web": {
                "results": [
                    {
                        "title": title,
                        "url": "https://example.test/dopyt?id=7&utm_source=mail",
                        "description": "Aktuálny dopyt na stavebné práce v Poprade.",
                        "extra_snippets": ["Termín realizácie je október."],
                        "page_age": "2 days ago",
                    },
                    {
                        "title": "Rovnaký výsledok",
                        "url": "https://example.test/dopyt?utm_medium=cpc&id=7#detail",
                        "description": "Duplikát výsledku.",
                    },
                ]
            }
        }

    def test_source_url_removes_tracking_but_keeps_identity_parameters(self):
        url = normalize_source_url(
            "HTTPS://Example.Test/job?id=9&utm_source=x&fbclid=abc#offer"
        )

        self.assertEqual(url, "https://example.test/job?id=9")

    def test_disabled_campaign_never_calls_search(self):
        campaign = self.add_campaign(enabled=False)
        search = Mock()

        result = run_campaign_scout(campaign, search=search, now=self.now)

        self.assertIn("skipped", result)
        search.assert_not_called()
        self.assertEqual(ScoutRun.query.count(), 0)

    def test_scout_stores_deduplicated_evidence_without_sending(self):
        campaign = self.add_campaign()
        search = Mock(return_value=self.payload())

        result = run_campaign_scout(campaign, search=search, now=self.now)

        self.assertEqual(result["discovered"], 1)
        self.assertEqual(Opportunity.query.count(), 1)
        opportunity = Opportunity.query.one()
        self.assertEqual(opportunity.business_line, "construction")
        self.assertEqual(opportunity.source_url, "https://example.test/dopyt?id=7")
        self.assertGreater(opportunity.fit_score, 20)
        self.assertEqual(opportunity.status, "new")
        self.assertEqual(opportunity.evidence, ["Termín realizácie je október."])
        search.assert_called_once_with(
            "stavebné práce dopyt Poprad",
            count=5,
            country="SK",
            search_lang="sk",
        )

    def test_same_campaign_runs_at_most_once_per_utc_day(self):
        campaign = self.add_campaign()
        search = Mock(return_value=self.payload())

        first = run_campaign_scout(campaign, search=search, now=self.now)
        second = run_campaign_scout(
            campaign,
            search=search,
            now=self.now + timedelta(hours=2),
        )

        self.assertEqual(first["discovered"], 1)
        self.assertIn("skipped", second)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(ScoutRun.query.count(), 1)

    def test_next_day_refreshes_existing_opportunity(self):
        campaign = self.add_campaign()
        search = Mock(return_value=self.payload())
        run_campaign_scout(campaign, search=search, now=self.now)
        search.return_value = self.payload(title="Aktualizovaný dopyt")

        result = run_campaign_scout(
            campaign,
            search=search,
            now=self.now + timedelta(days=1),
        )

        self.assertEqual(result["discovered"], 0)
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(Opportunity.query.count(), 1)
        self.assertEqual(Opportunity.query.one().title, "Aktualizovaný dopyt")

    def test_search_failure_is_recorded_and_not_retried_same_day(self):
        campaign = self.add_campaign()
        search = Mock(side_effect=TimeoutError("search timeout"))

        first = run_campaign_scout(campaign, search=search, now=self.now)
        second = run_campaign_scout(campaign, search=search, now=self.now)

        self.assertIn("error", first)
        self.assertIn("skipped", second)
        self.assertEqual(ScoutRun.query.one().status, "failed")
        self.assertEqual(Opportunity.query.count(), 0)
        self.assertEqual(search.call_count, 1)


if __name__ == "__main__":
    unittest.main()
