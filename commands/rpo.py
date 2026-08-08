import json

import click
from flask.cli import with_appcontext

from services.rpo_sync import enrich_company_contacts, sync_rpo


@click.command("sync-rpo")
@click.option(
    "--max-records",
    type=click.IntRange(min=1),
    default=None,
    help="Zastaví testovací import po zadanom počte záznamov.",
)
@click.option(
    "--only-ids",
    is_flag=True,
    default=False,
    help="Najprv stiahne iba ID a potom detail každého záznamu.",
)
@click.option(
    "--commit-every",
    type=click.IntRange(min=1),
    default=100,
    show_default=True,
    help="Počet záznamov medzi databázovými commitmi.",
)
@click.option(
    "--delay",
    type=click.FloatRange(min=0),
    default=1.1,
    show_default=True,
    help="Pauza medzi API požiadavkami v sekundách.",
)
@click.option(
    "--restart",
    is_flag=True,
    default=False,
    help="Ignoruje uloženú next_url a začne nový sync beh.",
)
@with_appcontext
def sync_rpo_command(
    max_records: int | None,
    only_ids: bool,
    commit_every: int,
    delay: float,
    restart: bool,
) -> None:
    """
    Synchronizuje právnické osoby z RPO V2.
    """

    click.echo("Spúšťam RPO2 synchronizáciu...")

    try:
        result = sync_rpo(
            max_records=max_records,
            only_ids=only_ids,
            commit_every=commit_every,
            delay_seconds=delay,
            resume=not restart,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@click.command("enrich-contacts")
@click.option(
    "--max-companies",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    help="Počet firiem, pre ktoré sa doplnia kontakty z Brave.",
)
@click.option(
    "--delay",
    type=click.FloatRange(min=0),
    default=0.5,
    show_default=True,
    help="Pauza medzi Brave dotazmi v sekundách.",
)
@click.option(
    "--include-existing",
    is_flag=True,
    default=False,
    help="Spracuje aj firmy, ktoré už majú aspoň jeden kontakt.",
)
@with_appcontext
def enrich_contacts_command(
    max_companies: int,
    delay: float,
    include_existing: bool,
) -> None:
    """Doplní kontakty pre RPO firmy z Brave vyhľadávania."""
    click.echo("Dopĺňam kontakty z Brave...")

    try:
        result = enrich_company_contacts(
            max_companies=max_companies,
            delay_seconds=delay,
            include_existing=include_existing,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
