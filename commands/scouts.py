import json

import click
from flask.cli import with_appcontext

from extensions import db
from models import Campaign
from services.opportunity_scout import run_campaign_scout, run_due_scouts


@click.command("run-scouts")
@click.option(
    "--campaign-id",
    type=click.IntRange(min=1),
    default=None,
    help="Spustí lov iba pre jednu aktívnu kampaň.",
)
@with_appcontext
def run_scouts_command(campaign_id):
    """Vyhľadá a uloží príležitosti; neposiela žiadne správy."""
    if campaign_id is None:
        results = run_due_scouts()
    else:
        campaign = db.session.get(Campaign, campaign_id)
        if campaign is None:
            raise click.ClickException("Kampaň neexistuje.")
        results = [run_campaign_scout(campaign)]

    click.echo(json.dumps(results, ensure_ascii=False, indent=2, default=str))
