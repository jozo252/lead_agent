"""Two real messages between the owner's accounts; isolated in-memory CRM.

Requires --send and a new receipt path. A prior receipt prevents uncertain retries.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--receipt', required=True)
    args = parser.parse_args()
    if not args.send:
        print(json.dumps({'would_send': 2, 'from': 'adam@gallax.io', 'to': 'elektro@gallax.io', 'database': 'in_memory_only'}))
        return
    receipt_path = Path(args.receipt)
    # Exclusive file creation before any SMTP makes retries reviewable.
    descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    result = {'from': 'adam@gallax.io', 'to': 'elektro@gallax.io', 'production_database_written': False,
              'smtp_sent': 0, 'received': [], 'status': 'started'}
    def save():
        receipt_path.write_text(json.dumps(result, indent=2), encoding='utf-8')
    save()
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    from app import create_app
    from extensions import db
    from models import Campaign, CampaignFollowUp, CampaignRecipient, Company, CompanyContact, OutboundEmail, SenderProfile
    from services.campaign_delivery import send_campaign_recipients
    from services.campaign_followups import run_due_followups
    from services.sender_profiles import fetch_profile_messages, profile_readiness
    app = create_app({'APP_ENV': 'testing', 'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite://',
                      'MAIL_SUPPRESS_SEND': False, 'WTF_CSRF_ENABLED': False})
    started = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        with app.app_context():
            db.create_all()
            sender = SenderProfile(name='Internal selftest', config_key='WEBS', sender_name='Adam Gallik',
                                   sender_email='adam@gallax.io', signature='Interný test Lead Agent', enabled=True)
            receiver = SenderProfile(name='Owner recipient', config_key='ELEKTRO', sender_name='Adam Gallik',
                                     sender_email='elektro@gallax.io', enabled=True)
            assert not profile_readiness(sender, require_imap=True), 'Sender not ready'
            assert not profile_readiness(receiver, require_imap=True), 'Receiver not ready'
            campaign = Campaign(name='Internal delivery selftest', offer_description='Interný test',
                                subject_template='Lead Agent - interný test automatiky', body_template='Interný test vlastných schránok.',
                                sender_profile=sender, status='active', daily_limit=2, target_total=1,
                                follow_up_enabled=True, follow_up_days=7, follow_up_approved_at=started,
                                follow_up_subject_template='Re: Lead Agent - interný test automatiky',
                                follow_up_body_template='Interný test jedného pripomenutia. Test zrýchlil iba vlastnú dočasnú databázu.')
            company = Company(official_name='Vlastná testovacia schránka')
            contact = CompanyContact(company=company, contact_type='email', value=receiver.sender_email,
                                     source_type='owner_selftest', is_verified=True)
            recipient = CampaignRecipient(campaign=campaign, company=company, contact=contact,
                                          recipient_email=receiver.sender_email, subject=campaign.subject_template,
                                          body=campaign.body_template, status='approved', approved_at=started)
            db.session.add_all([recipient, receiver])
            db.session.commit()
            result['initial_attempted'] = True
            save()
            initial = send_campaign_recipients(campaign, 1)
            assert initial['sent'] == 1, 'Initial SMTP not confirmed; do not retry'
            result['smtp_sent'] = 1
            original = OutboundEmail.query.one()
            result['initial_message_id'] = original.message_id
            reminder = CampaignFollowUp.query.one()
            assert reminder.due_at - original.sent_at == timedelta(days=7)
            result['followup_delay_verified_days'] = 7
            save()

            def wait_received(message_id):
                for attempt in range(8):
                    messages = fetch_profile_messages(receiver, since_datetime=started - timedelta(minutes=1))
                    for message in messages:
                        if message.get('message_id') == message_id:
                            result['received'].append(message_id)
                            save()
                            return message
                    if attempt < 7:
                        time.sleep(5)
                raise RuntimeError('Arrival not verified within 40 seconds')

            wait_received(original.message_id)
            reminder.due_at = started - timedelta(seconds=1)
            db.session.commit()
            result['followup_attempted'] = True
            save()
            followup = run_due_followups(dry_run=False, campaign_id=campaign.id, limit=1)
            assert followup['sent'] == 1, 'Follow-up SMTP not confirmed; do not retry'
            result['smtp_sent'] = 2
            result['followup_message_id'] = reminder.message_id
            save()
            received = wait_received(reminder.message_id)
            assert original.message_id in received.get('thread_message_ids', []), 'Thread headers missing'
            assert run_due_followups(dry_run=False, campaign_id=campaign.id, limit=1)['sent'] == 0
            assert OutboundEmail.query.count() == 2
            result['threading_verified'] = True
            result['repeat_sent'] = 0
            result['status'] = 'passed'
    except Exception as exc:
        result['status'] = 'failed_or_uncertain'
        result['error_type'] = type(exc).__name__
        save()
        print(json.dumps(result))
        return 1
    save()
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
