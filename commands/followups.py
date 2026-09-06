import json

import click
from flask.cli import with_appcontext

from services.campaign_followups import run_due_followups


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
