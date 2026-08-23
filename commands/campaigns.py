import json

import click
from flask.cli import with_appcontext

from services.campaign_automation import run_due_campaigns


@click.command("run-campaigns")
@click.option(
    "--campaign-id",
    type=click.IntRange(min=1),
    default=None,
    help="Spustí iba jednu aktívnu automatickú kampaň.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Povolí opakovať dávku v rovnaký deň; stále platia denné a celkové limity.",
)
@with_appcontext
def run_campaigns_command(campaign_id, force):
    """Spracuje jednu dennú dávku aktívnych automatických kampaní."""
    results = run_due_campaigns(force=force, campaign_id=campaign_id)
    click.echo(json.dumps(results, ensure_ascii=False, indent=2, default=str))
