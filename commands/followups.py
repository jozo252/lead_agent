import json

import click
from flask.cli import with_appcontext

from extensions import db
from models import Campaign
from services.campaign_followups import run_due_followups, sync_profile_inbox


@click.command("run-followups")
@click.option("--send", is_flag=True, default=False,
              help="Skutočne odošle iba schválené follow-upy po čerstvej kontrole inboxu.")
@click.option("--campaign-id", type=click.IntRange(min=1), default=None)
@click.option("--limit", type=click.IntRange(min=1, max=100), default=20)
@with_appcontext
def run_followups_command(send, campaign_id, limit):
    """Bez --send iba zobrazí frontu; nemení dáta ani nevolá SMTP/IMAP."""
    result = run_due_followups(dry_run=not send, campaign_id=campaign_id, limit=limit)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@click.command("sync-campaign-inbox")
@click.option("--campaign-id", type=click.IntRange(min=1), required=True)
@with_appcontext
def sync_campaign_inbox_command(campaign_id):
    """Uloží odpovede a odhlásenia zo schránky kampane; nikdy neposiela e-mail.

    Kontrola je povolená aj pri pozastavenej kampani. Celé relevantné okno
    spoločného profilu musí byť načítané pred pokračovaním schedulera.
    """
    result = {"campaign_id": campaign_id, "synced": False, "sent": 0}
    try:
        campaign = db.session.get(Campaign, campaign_id)
        if campaign is None:
            result["error"] = "Kampaň neexistuje."
        elif campaign.sender_profile is None:
            result["error"] = "Kampaň nemá vlastný profil odosielateľa."
        else:
            result["sender_profile_id"] = campaign.sender_profile_id
            # No since_datetime: the service scans from the earliest outbound
            # in this account, including late replies after the final reminder.
            result["inbox"] = sync_profile_inbox(campaign.sender_profile)
            result["synced"] = True
    except Exception:
        db.session.rollback()
        result["error"] = "Úplná kontrola schránky zlyhala; scheduler nesmie pokračovať v odosielaní."
    click.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result["synced"]:
        raise click.exceptions.Exit(1)
