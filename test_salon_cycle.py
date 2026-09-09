import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import create_app
from extensions import db
from models import Campaign, CampaignFollowUp, CampaignRecipient, Company, CompanyContact, OutboundEmail, SenderProfile
from services.campaign_delivery import send_campaign_recipients
from services.salon_cycle import run_salon_cycle


class SalonCycleTests(unittest.TestCase):
    def setUp(self):
        settings = {'MAIL_SERVER': 'smtp.example.com', 'MAIL_PORT': 587, 'MAIL_USE_TLS': True,
                    'MAIL_USERNAME': 'webs@example.com', 'MAIL_PASSWORD': 'test',
                    'IMAP_SERVER': 'imap.example.com', 'IMAP_USERNAME': 'webs@example.com', 'IMAP_PASSWORD': 'test'}
        self.app = create_app({'TESTING': True, 'APP_ENV': 'testing', 'SECRET_KEY': 'test',
                              'SQLALCHEMY_DATABASE_URI': 'sqlite://', 'MAIL_SUPPRESS_SEND': True,
                              'SENDER_PROFILE_SETTINGS': {'WEBS': settings}})
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.profile = SenderProfile(name='Test', config_key='WEBS', sender_name='Test', sender_email='webs@example.com', enabled=True)
        self.campaign = Campaign(name='Salony', offer_description='Pripravované objednávanie',
                                 subject_template='Web pre {company_name}', body_template='Dobrý deň,\nPripravujem web.',
                                 status='active', automation_enabled=True, sender_profile=self.profile,
                                 daily_limit=3, batch_size=3, target_total=2, follow_up_enabled=True,
                                 follow_up_approved_at=datetime.now(), follow_up_days=7,
                                 follow_up_subject_template='Re: Web pre {company_name}', follow_up_body_template='Pripomínam ukážku.',
                                 targeting_profile={'salon_discovery': True, 'company_keywords': ['salon'],
                                                    'location_keywords': ['Poprad'], 'copy_mode': 'fixed_template',
                                                    'privacy_notice_url': 'https://gallax.io/informacie-o-spracuvani-osobnych-udajov'})
        db.session.add(self.campaign)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def discover(self, campaign, **kwargs):
        for number in range(2):
            company = Company(official_name=f'Salon {number}', municipality='Poprad', contacts_checked_at=datetime.now())
            db.session.add(CompanyContact(company=company, contact_type='email', value=f'salon{number}@example.com',
                                         source_type='salon_public_listing', source_url=f'https://salon{number}.sk/kontakt/',
                                         is_verified=True, last_verified_at=datetime.now(timezone.utc)))
        campaign.last_run_summary = {'salon_discovery_state': {'visited': ['https://salon0.sk/kontakt/']}}
        db.session.flush()
        return {'eligible': 2, 'imported': 2, 'searched': 1}

    @patch('services.salon_cycle.sync_profile_inbox')
    @patch('services.salon_cycle.run_campaign_automation')
    def test_preview_and_disabled_cycle_never_contact_network(self, run, sync):
        before = (CampaignRecipient.query.count(), self.profile.last_synced_at, self.campaign.last_run_summary)
        self.assertTrue(run_salon_cycle(self.campaign.id)['dry_run'])
        self.campaign.automation_enabled = False
        self.assertIn('skipped', run_salon_cycle(self.campaign.id, send=True))
        self.assertEqual(before, (CampaignRecipient.query.count(), self.profile.last_synced_at, self.campaign.last_run_summary))
        sync.assert_not_called()
        run.assert_not_called()

    @patch('services.salon_cycle.sync_profile_inbox', side_effect=ValueError('Incomplete inbox'))
    @patch('services.salon_cycle.run_due_followups')
    @patch('services.salon_cycle.run_campaign_automation')
    def test_incomplete_inbox_blocks_whole_cycle(self, run, followups, sync):
        with self.assertRaises(ValueError):
            run_salon_cycle(self.campaign.id, send=True)
        run.assert_not_called()
        followups.assert_not_called()

    @patch('services.campaign_delivery.send_profile_message')
    @patch('services.campaign_followups.send_profile_message')
    @patch('services.campaign_followups.fetch_profile_messages', return_value=[])
    def test_discovery_to_initial_to_followup_shared_limit_and_no_duplicates(self, fetch, follow_send, initial_send):
        with patch('services.salon_discovery.discover_salon_contacts', side_effect=self.discover) as discover, \
                patch('services.salon_discovery.recheck_salon_contact', return_value=True):
            first = run_salon_cycle(self.campaign.id, send=True)
            self.assertEqual(first['sent'], 2)
            self.assertEqual(initial_send.call_count, 2)
            self.assertEqual(CampaignFollowUp.query.count(), 2)
            self.assertEqual(self.campaign.status, 'active')
            self.assertIn('salon_discovery_state', self.campaign.last_run_summary)
            for reminder in CampaignFollowUp.query.all():
                self.assertEqual(reminder.due_at - reminder.original_outbound.sent_at, timedelta(days=7))
                reminder.due_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            db.session.commit()
            second = run_salon_cycle(self.campaign.id, send=True)
            self.assertEqual(second['followups']['sent'], 1, second)
            self.assertEqual(second['sent'], 1)
            self.assertEqual(OutboundEmail.query.count(), 3)
            self.assertEqual(CampaignFollowUp.query.filter_by(status='scheduled').count(), 1)
            third = run_salon_cycle(self.campaign.id, send=True)
            self.assertEqual(third['sent'], 0)
            self.assertEqual(OutboundEmail.query.count(), 3)
            self.assertEqual(discover.call_count, 1)

    @patch('services.campaign_delivery.send_profile_message')
    def test_initial_requires_fresh_successful_inbox_sync(self, send):
        self.assertEqual(send_campaign_recipients(self.campaign, 1)['sent'], 0)
        self.profile.last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=6)
        self.assertEqual(send_campaign_recipients(self.campaign, 1)['sent'], 0)
        send.assert_not_called()

    @patch('services.campaign_delivery.send_profile_message')
    def test_empty_privacy_url_cannot_bypass_delivery_guard(self, send):
        self.discover(self.campaign)
        contact = CompanyContact.query.first()
        contact.source_url = None
        self.profile.last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.campaign.targeting_profile = {**self.campaign.targeting_profile, 'privacy_notice_url': ''}
        db.session.add(CampaignRecipient(campaign=self.campaign, company=contact.company, contact=contact,
                                        recipient_email=contact.value, subject='Test', body='Test',
                                        status='approved', approved_at=datetime.now()))
        db.session.commit()
        self.assertIn('Chýba povinný odkaz', send_campaign_recipients(self.campaign, 1)['message'])
        send.assert_not_called()

    @patch('services.campaign_delivery.send_profile_message')
    @patch('services.campaign_followups.send_profile_message')
    @patch('services.campaign_followups.fetch_profile_messages', return_value=[])
    def test_followup_stops_when_public_source_cannot_be_reverified(self, fetch, follow_send, initial_send):
        with patch('services.salon_discovery.discover_salon_contacts', side_effect=self.discover):
            run_salon_cycle(self.campaign.id, send=True)
        for row in CampaignFollowUp.query.all():
            row.due_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        db.session.commit()
        with patch('services.salon_discovery.recheck_salon_contact', return_value=False) as recheck:
            result = run_salon_cycle(self.campaign.id, send=True)
        self.assertEqual(result['followups']['sent'], 0)
        self.assertEqual(result['followups']['blocked'], 2)
        self.assertEqual(recheck.call_count, 2)
        follow_send.assert_not_called()

    @patch('services.campaign_delivery.send_profile_message')
    @patch('services.campaign_followups.fetch_profile_messages', return_value=[])
    def test_slow_discovery_refreshes_inbox_before_daily_send_stamp(self, fetch, initial_send):
        def slow_discovery(campaign, **kwargs):
            result = self.discover(campaign)
            self.profile.last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=6)
            return result
        with patch('services.salon_discovery.discover_salon_contacts', side_effect=slow_discovery):
            result = run_salon_cycle(self.campaign.id, send=True)
        self.assertEqual(result['sent'], 2)
        self.assertEqual(fetch.call_count, 2)

    @patch('services.campaign_followups.fetch_profile_messages', return_value=[])
    @patch('services.salon_cycle.run_due_followups')
    @patch('services.salon_cycle.run_campaign_automation')
    def test_collection_saves_new_contacts_but_never_sends(self, automation, followups, fetch):
        self.campaign.status = 'draft'
        self.campaign.automation_enabled = False
        with patch('services.salon_discovery.discover_salon_contacts', side_effect=self.discover):
            result = run_salon_cycle(self.campaign.id, collect_only=True)
        self.assertEqual(result['sent'], 0)
        self.assertEqual(result['discovery']['imported'], 2)
        self.assertEqual(CompanyContact.query.count(), 2)
        self.assertEqual(OutboundEmail.query.count(), 0)
        automation.assert_not_called()
        followups.assert_not_called()
