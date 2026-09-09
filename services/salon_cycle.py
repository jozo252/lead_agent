"""One bounded salon worker: inbox, due reminders, discovery, initial batch."""
from datetime import datetime, timezone

from extensions import db
from models import Campaign, CampaignRecipient
from services.campaign_automation import run_campaign_automation
from services.campaign_followups import run_due_followups, sync_profile_inbox
from services.sender_profiles import profile_readiness


def run_salon_cycle(campaign_id, *, send=False, collect_only=False):
    campaign = db.session.get(Campaign, campaign_id)
    if campaign is None or (campaign.targeting_profile or {}).get('salon_discovery') is not True:
        raise ValueError('Kampaň nemá zapnuté vyhľadávanie salónov.')
    if send and collect_only:
        raise ValueError('Vyber iba jeden režim: zber alebo odosielanie.')
    result = {'campaign_id': campaign.id, 'dry_run': not send and not collect_only, 'collect_only': collect_only, 'sent': 0}
    if not send and not collect_only:
        result['status'] = campaign.status
        result['automation_enabled'] = bool(campaign.automation_enabled)
        result['followups'] = run_due_followups(campaign_id=campaign.id)
        result['message'] = 'Náhľad nemení údaje a nevolá sieť. Vyhľadávanie otestuj príkazom discover-salons.'
        return result
    if send and (campaign.status != 'active' or not campaign.automation_enabled):
        result['skipped'] = 'Kampaň nie je aktívna alebo automatizácia nie je zapnutá.'
        return result
    if not (campaign.targeting_profile or {}).get('privacy_notice_url'):
        raise ValueError('Chýba odkaz na informácie o spracúvaní údajov.')
    if campaign.sender_profile is None or profile_readiness(campaign.sender_profile, require_imap=True):
        raise ValueError('Profil kampane nemá pripravený SMTP a IMAP účet.')
    # This failure must stop the whole cycle, including first emails.
    result['inbox'] = sync_profile_inbox(campaign.sender_profile)
    if send:
        result['followups'] = run_due_followups(dry_run=False, campaign_id=campaign.id, limit=100)
        result['sent'] += result['followups']['sent']
        if result['followups']['unknown']:
            result['error'] = 'Neistý výsledok follow-upu; nové oslovenia v tomto behu zastavené.'
            return result
    now = datetime.now(timezone.utc)
    already_sent = CampaignRecipient.query.filter(
        CampaignRecipient.campaign_id == campaign.id,
        CampaignRecipient.sent_at.isnot(None),
    ).count()
    if (already_sent < campaign.target_total and
            (campaign.last_scout_run_at is None or campaign.last_scout_run_at.date() != now.date())):
        from services.salon_discovery import discover_salon_contacts
        result['discovery'] = discover_salon_contacts(campaign, dry_run=False, limit=12)
        campaign.last_scout_run_at = now
        campaign.last_scout_error = None
        if result['discovery']['searched'] == 0:
            campaign.last_scout_error = 'Verejné vyhľadávanie zlyhalo.'
            result['error'] = campaign.last_scout_error
        db.session.commit()
    if collect_only or result.get('error'):
        return result
    discovery_state = (campaign.last_run_summary or {}).get('salon_discovery_state')
    # Public searches can take minutes. Refresh immediately before the daily
    # send stamp so an old inbox snapshot cannot consume today's batch.
    result['inbox_before_initials'] = sync_profile_inbox(campaign.sender_profile)
    result['initials'] = run_campaign_automation(campaign)
    result['sent'] += (result['initials'].get('delivery') or {}).get('sent', 0)
    if discovery_state is not None:
        campaign.last_run_summary = {**(campaign.last_run_summary or {}), 'salon_discovery_state': discovery_state}
        db.session.commit()
    if result['initials'].get('error'):
        result['error'] = 'Automatická dávka zlyhala; pozri stav kampane.'
    return result
