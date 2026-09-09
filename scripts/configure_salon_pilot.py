"""Idempotently configure existing salon pilot for collection, without sending.

Rehearse against a consistent backup first. Credentials stay in .env.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def protected_snapshot(path):
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as connection:
        tables = ('campaign_recipients', 'outbound_emails', 'campaign_followups', 'lead', 'email_reply', 'suppressions')
        values = {table: connection.execute(f'SELECT * FROM {table} ORDER BY id').fetchall() for table in tables}
        values['other_campaigns'] = connection.execute('SELECT * FROM campaigns WHERE id != 6 ORDER BY id').fetchall()
        values['other_senders'] = connection.execute('SELECT * FROM sender_profiles WHERE id != 1 ORDER BY id').fetchall()
        return hashlib.sha256(json.dumps(values, default=str, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    path = Path(args.database).resolve(strict=True)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    from app import create_app
    from extensions import db
    from models import Campaign
    before = protected_snapshot(path)
    app = create_app({'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + path.as_posix()})
    with app.app_context():
        campaign = db.session.get(Campaign, 6)
        assert campaign and campaign.name.startswith('Salóny')
        assert campaign.sender_profile_id == 1 and campaign.sender_profile.sender_email == 'adam@gallax.io'
        assert not campaign.automation_enabled, 'Do not rewrite a running campaign'
        if not args.apply:
            print(json.dumps({'campaign_id': 6, 'mode': 'collection_only', 'planned_target_total': 15,
                              'daily_limit': 3, 'batch_size': 3, 'followup_days': 7, 'production_database_written': False}))
            return
        campaign.status = 'draft'
        campaign.completed_at = None
        campaign.target_total = 15
        campaign.batch_size = 3
        campaign.daily_limit = 3
        campaign.contact_cooldown_days = 90
        campaign.automation_enabled = False
        campaign.scout_enabled = False  # Opportunity scout is a separate workflow.
        campaign.follow_up_enabled = False
        campaign.offer_stage = 'validation'
        campaign.require_no_website = False
        campaign.targeting_profile = {
            'salon_discovery': True, 'copy_mode': 'fixed_template',
            'company_keywords': ['salón', 'salon', 'kozmetika', 'beauty', 'kaderníctvo', 'pedikúra'],
            'location_keywords': ['Poprad', 'Svit', 'Kežmarok'],
            'minimum_fit_score': 80,
            'privacy_notice_url': 'https://gallax.io/informacie-o-spracuvani-osobnych-udajov',
            'release_state': 'awaiting_service_scope_confirmation',
        }
        campaign.subject_template = 'Ukážka webu a objednávania pre {company_name}'
        campaign.body_template = (
            'Dobrý deň,\n\npripravil som ukážku jednoduchého webu a online objednávania pre salóny. '
            'Klient si pozrie služby a ceny a vyberie termín; salón má kalendár a môže blokovať čas.\n\n'
            'Ukážka: https://ukazka.gallax.io/salony-demo\n\n'
            'Ide o pripravované riešenie. Ak už rezervačný systém používate, dá sa najprv posúdiť prepojenie s webom. '
            'Ak vás to zaujalo, stačí odpovedať a dohodneme krátku ukážku.\n\n'
            'Ak si neželáte ďalšie správy, stačí odpovedať „neposielať“.'
        )
        campaign.follow_up_days = 7
        campaign.follow_up_subject_template = 'Re: Ukážka webu a objednávania pre {company_name}'
        campaign.follow_up_body_template = (
            'Dobrý deň,\n\nozývam sa ešte raz k ukážke webu a online objednávania pre váš salón: '
            'https://ukazka.gallax.io/salony-demo\n\n'
            'Ak je to pre vás aktuálne, stačí odpovedať. Ak nie, ďalšie pripomenutie neposielam.\n\n'
            'Informácie o spracúvaní údajov: https://gallax.io/informacie-o-spracuvani-osobnych-udajov\n\n'
            'Ak si neželáte ďalšie správy, stačí odpovedať „neposielať“.'
        )
        campaign.follow_up_approved_at = None
        campaign.sender_profile.sender_name = 'Adam Gallik'
        campaign.sender_profile.signature = (
            'Adam Gallik\nadam@gallax.io\nIČO: 54667640\nRovinky 1077/15, 059 07 Lendak\n'
            'Živnostenský register, Okresný úrad Kežmarok, č. 730-19971'
        )
        db.session.commit()
        assert protected_snapshot(path) == before, 'Protected campaign history changed'
        print(json.dumps({'campaign_id': 6, 'status': campaign.status, 'automation_enabled': False,
                          'follow_up_enabled': False, 'target_total_including_previous_six': 15,
                          'protected_history_unchanged': True, 'emails_sent': 0}))


if __name__ == '__main__':
    main()
