import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from app import create_app
from extensions import db
from models import Campaign, Company, CompanyContact, CompanyWebsiteCheck
from services.campaign_ai import rank_campaign_candidates
from services.campaign_automation import campaign_candidates, prepare_automated_recipients, website_candidate_pool
from services.company_filtering import company_filters_from_source, filtered_companies_query
from services.website_presence import (
    check_company_website,
    is_website_absence_eligible,
    website_absence_filter,
    website_presence_summary,
)


class WebsitePresenceTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "WTF_CSRF_ENABLED": False,
            "MAIL_SUPPRESS_SEND": True,
        })
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.company = Company(
            official_name="Pekné Stolárstvo, s. r. o.", ico="12345678",
            municipality="Žilina", sk_nace_code="1623",
        )
        db.session.add(self.company)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def result(self, url="https://finstat.sk/12345678", **overrides):
        result = {
            "url": url,
            "title": "Pekné Stolárstvo, s. r. o. Žilina",
            "description": "IČO: 12345678. Stolárske práce.",
        }
        result.update(overrides)
        return result

    @staticmethod
    def payload(*results):
        return {"web": {"results": list(results)}}

    def check(self, *results):
        search = Mock(return_value=self.payload(*results))
        result = check_company_website(self.company, search=search, now=self.now)
        db.session.flush()
        return result, search

    def campaign(self, require_no_website=True):
        campaign = Campaign(
            name="Weby", offer_type="service", offer_description="Tvorba webov",
            subject_template="Ponuka", body_template="Pripravíme jednoduchý web.",
            require_no_website=require_no_website,
            targeting_profile={"nace_keywords": ["1623"]},
        )
        db.session.add(campaign)
        db.session.commit()
        return campaign

    def add_check(self, status="not_found", age_days=0):
        check = CompanyWebsiteCheck(
            company=self.company, status=status,
            checked_at=(self.now - timedelta(days=age_days)).replace(tzinfo=None),
            searched_queries=["test identity query"], evidence=[{"url": "https://finstat.sk/12345678"}],
        )
        db.session.add(check)
        db.session.commit()
        return check

    def test_missing_db_contact_is_unknown_and_not_eligible(self):
        summary = website_presence_summary(self.company, now=self.now)
        self.assertEqual(summary["status"], "unknown")
        self.assertFalse(summary["eligible"])
        self.assertEqual(Company.query.filter(website_absence_filter(now=self.now)).count(), 0)

    def test_identified_directory_only_is_dated_search_not_absolute_absence(self):
        check, search = self.check(self.result())
        self.assertEqual(check.status, "not_found")
        self.assertTrue(is_website_absence_eligible(self.company, now=self.now))
        self.assertEqual(search.call_count, 3)
        self.assertEqual(len(check.searched_queries), 3)
        self.assertEqual(check.evidence[0]["identity_reason"], "ico")
        self.assertIn("nie dôkaz", website_presence_summary(self.company, now=self.now)["label"])
        self.assertEqual(Company.query.filter(website_absence_filter(now=self.now)).count(), 1)

    def test_empty_search_results_remain_unknown(self):
        check, _ = self.check()
        self.assertEqual(check.status, "unknown")
        self.assertFalse(is_website_absence_eligible(self.company, now=self.now))

    def test_only_unrelated_search_results_remain_unknown(self):
        check, _ = self.check(self.result(title="Iná firma Bratislava", description="IČO 99123456"))
        self.assertEqual(check.status, "unknown")

    def test_matching_company_on_official_candidate_domain_blocks_absence(self):
        check, _ = self.check(self.result(url="https://pekne-stolarstvo.sk/kontakt"))
        self.assertEqual(check.status, "found")
        self.assertEqual(check.website_url, "https://pekne-stolarstvo.sk/kontakt")
        self.assertFalse(is_website_absence_eligible(self.company, now=self.now))

    def test_name_locality_without_ico_recognizes_diacritics(self):
        check, _ = self.check(self.result(description="Stolarstvo Zilina", title="Pekne Stolarstvo"))
        self.assertEqual(check.status, "not_found")
        self.assertEqual(check.evidence[0]["identity_reason"], "name_locality")

    def test_ambiguous_name_without_locality_blocks_negative(self):
        check, _ = self.check(
            self.result(),
            self.result(url="https://pekne-stolarstvo.sk", title="Pekne Stolarstvo", description="Nábytok"),
        )
        self.assertEqual(check.status, "unknown")

    def test_similar_ico_is_not_identity_match(self):
        check, _ = self.check(self.result(title="Úplne iná firma", description="IČO 9123456789"))
        self.assertEqual(check.status, "unknown")

    def test_unresolved_brand_domain_alongside_registry_stays_unknown(self):
        check, _ = self.check(
            self.result(),
            self.result(url="https://nabytok-znacka.example.test", title="Furniture brand", description="Nábytok na mieru"),
        )
        self.assertEqual(check.status, "unknown")
        self.assertTrue(any(item["kind"] == "potential_website" for item in check.evidence))

    def test_social_and_bazos_results_are_not_official_website(self):
        for url in ["https://www.facebook.com/pekne-stolarstvo", "https://sluzby.bazos.sk/inzerat/7"]:
            with self.subTest(url=url):
                check, _ = self.check(self.result(url=url))
                self.assertEqual(check.status, "not_found")
                self.assertIsNone(check.website_url)

    def test_link_in_directory_snippet_leaves_uncertainty(self):
        check, _ = self.check(self.result(description="IČO 12345678; www.pekne-stolarstvo.sk"))
        self.assertEqual(check.status, "unknown")

    def test_domain_suffix_cannot_impersonate_directory(self):
        check, _ = self.check(self.result(url="https://finstat.sk.example.test/12345678"))
        self.assertEqual(check.status, "found")

    def test_existing_unverified_website_excludes_without_search(self):
        contact = CompanyContact(company=self.company, contact_type="website", value="https://old.example.test", source_type="test", is_verified=False)
        db.session.add(contact)
        db.session.flush()
        search = Mock()
        check = check_company_website(self.company, search=search, now=self.now)
        self.assertEqual(check.status, "found")
        search.assert_not_called()

    def test_malformed_or_partial_response_fails_closed_and_sanitizes_errors(self):
        for response in [{}, {"web": {}}, self.payload({"url": "javascript:alert(1)"})]:
            with self.subTest(response=response):
                search = Mock(return_value=response)
                check = check_company_website(self.company, search=search, now=self.now)
                self.assertEqual(check.status, "error")
        search = Mock(side_effect=[self.payload(self.result()), RuntimeError("secret-token-123")])
        check = check_company_website(self.company, search=search, now=self.now)
        self.assertEqual(check.status, "error")
        self.assertNotIn("secret-token-123", check.last_error)
        self.assertFalse(is_website_absence_eligible(self.company, now=self.now))

    def test_insufficient_identity_does_not_call_search(self):
        self.company.municipality = None
        self.company.ico = None
        search = Mock()
        check = check_company_website(self.company, search=search, now=self.now)
        self.assertEqual(check.status, "unknown")
        search.assert_not_called()

    def test_service_does_not_commit(self):
        with patch.object(db.session, "commit") as commit:
            self.check(self.result())
        commit.assert_not_called()

    def test_stale_and_future_checks_exclude_in_python_and_sql(self):
        check = self.add_check(age_days=31)
        for checked_at in [self.now - timedelta(days=31), self.now + timedelta(hours=1), None]:
            with self.subTest(checked_at=checked_at):
                check.checked_at = checked_at
                db.session.flush()
                self.assertFalse(is_website_absence_eligible(self.company, now=self.now))
                self.assertEqual(Company.query.filter(website_absence_filter(now=self.now)).count(), 0)

    def test_new_stored_website_overrides_fresh_negative(self):
        self.add_check()
        db.session.add(CompanyContact(company=self.company, contact_type=" Website ", value="https://example.test", source_type="test"))
        db.session.flush()
        self.assertFalse(is_website_absence_eligible(self.company, now=self.now))
        self.assertEqual(website_presence_summary(self.company)["status"], "found")
        self.assertEqual(Company.query.filter(website_absence_filter(now=self.now)).count(), 0)

    def test_automatic_selection_requires_fresh_negative_but_legacy_unchanged(self):
        campaign = self.campaign()
        with patch("services.website_presence.brave_web_search") as search:
            self.assertEqual(campaign_candidates(campaign, 5), [])
            campaign.require_no_website = False
            self.assertEqual(campaign_candidates(campaign, 5), [self.company])
            campaign.require_no_website = True
            self.add_check()
            self.assertEqual(campaign_candidates(campaign, 5), [self.company])
        search.assert_not_called()

    def test_explicit_candidate_pool_is_bounded_and_excludes_fresh_checks(self):
        campaign = self.campaign()
        for index in range(15):
            db.session.add(Company(official_name=f"Stolárstvo {index}", ico=f"990000{index:02d}", sk_nace_code="1623", municipality="Žilina"))
        db.session.commit()
        with patch("services.website_presence.brave_web_search") as search:
            self.assertEqual(len(website_candidate_pool(campaign, 100)), 10)
            self.assertEqual(website_candidate_pool(campaign, 0), [])
            self.add_check()
            self.assertNotIn(self.company, website_candidate_pool(campaign, 10))
        search.assert_not_called()

    def test_manual_filter_retains_legacy_missing_and_adds_checked_negative(self):
        filters = company_filters_from_source({"website": "no"})
        self.assertEqual(filtered_companies_query(filters).count(), 1)
        filters["website"] = "checked_not_found"
        self.assertEqual(filtered_companies_query(filters).count(), 0)
        self.add_check()
        self.assertEqual(filtered_companies_query(filters).count(), 1)

    def test_ai_gate_never_calls_llm_for_unknown_web(self):
        campaign = self.campaign()
        with patch("services.campaign_ai._client") as client:
            self.assertEqual(rank_campaign_candidates(campaign, [self.company]), [])
        client.assert_not_called()

    def test_candidate_pool_rotates_past_recent_unknown(self):
        campaign = self.campaign()
        self.add_check(status="unknown")
        unsearched = Company(official_name="Druhé Stolárstvo", ico="99000001", sk_nace_code="1623", municipality="Žilina")
        db.session.add(unsearched)
        db.session.commit()
        self.assertEqual(website_candidate_pool(campaign, 1), [unsearched])

    def test_enrichment_discovering_website_prevents_draft_or_approval(self):
        campaign = self.campaign()
        self.add_check()

        def enrich(**kwargs):
            db.session.add(CompanyContact(company=self.company, contact_type="website", value="https://example.test", source_type="test"))
            db.session.add(CompanyContact(company=self.company, contact_type="email", value="hello@example.test", source_type="test", is_verified=True))
            db.session.commit()

        assessment = [{"company_id": self.company.id, "fit_score": 80, "fit_reason": "Segment", "subject": "Test", "body": "Test"}]
        with patch("services.campaign_automation.rank_campaign_candidates", return_value=assessment), patch("services.campaign_automation.enrich_company_contacts", side_effect=enrich):
            result = prepare_automated_recipients(campaign, 1)
        self.assertEqual(result["prepared"], 0)


if __name__ == "__main__":
    unittest.main()
