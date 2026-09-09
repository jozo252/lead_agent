import json

import click
from flask.cli import with_appcontext

from extensions import db
from models import Campaign
from services.salon_cycle import run_salon_cycle


@click.command('discover-salons')
@click.option('--campaign-id', type=click.IntRange(min=1), required=True)
@click.option('--save', is_flag=True, help='Uloží overené kontakty, nikdy neposiela e-mail.')
@click.option('--limit', type=click.IntRange(min=1, max=12), default=12)
@with_appcontext
def discover_salons_command(campaign_id, save, limit):
    """Živé verejné vyhľadávanie; bez --save zostáva databáza nezmenená."""
    campaign = db.session.get(Campaign, campaign_id)
    if campaign is None or (campaign.targeting_profile or {}).get('salon_discovery') is not True:
        raise click.ClickException('Kampaň nemá zapnuté vyhľadávanie salónov.')
    try:
        from services.salon_discovery import discover_salon_contacts
        result = discover_salon_contacts(campaign, dry_run=not save, limit=limit)
        if save:
            db.session.commit()
        click.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    except Exception:
        db.session.rollback()
        raise click.ClickException('Vyhľadávanie salónov zlyhalo; neodoslal sa žiadny e-mail.') from None


@click.command('run-salon-cycle')
@click.option('--campaign-id', type=click.IntRange(min=1), required=True)
@click.option('--send', is_flag=True, help='Spracuje zapnutú kampaň vrátane odosielania.')
@click.option('--collect-only', is_flag=True, help='Skontroluje inbox a uloží nové kontakty; neposiela.')
@with_appcontext
def run_salon_cycle_command(campaign_id, send, collect_only):
    """Bez --send iba náhľad bez siete a zápisu; jeden worker na VPS."""
    try:
        result = run_salon_cycle(campaign_id, send=send, collect_only=collect_only)
    except Exception:
        db.session.rollback()
        click.echo(json.dumps({'campaign_id': campaign_id, 'error': 'Cyklus zlyhal; ďalšie odosielanie zastavené.'}, ensure_ascii=False))
        raise click.exceptions.Exit(1)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if result.get('error'):
        raise click.exceptions.Exit(1)
