import json

import click
from flask.cli import with_appcontext

from services.rpo_sync import (
    backfill_rpo_company_fields,
    enrich_company_contacts,
    sync_rpo,
)
from services.company_web_enrichment import enrich_company_websites
from services.ruz_financials import enrich_company_financials


@click.command("sync-rpo")
@click.option(
    "--max-records",
    type=click.IntRange(min=1),
    default=None,
    help=(
        "Zastaví import po celej stránke po dosiahnutí limitu "
        "načítaných RPO záznamov."
    ),
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
    help="Vynúti opätovnú kontrolu všetkých firiem vrátane už skontrolovaných.",
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


@click.command("enrich-financials")
@click.option(
    "--max-companies",
    type=click.IntRange(min=1),
    default=10,
    show_default=True,
    help="Počet firiem, pre ktoré sa načítajú financie z RÚZ.",
)
@click.option(
    "--include-existing",
    is_flag=True,
    default=False,
    help="Vynúti opätovnú kontrolu už spracovaných firiem.",
)
@click.option(
    "--delay",
    type=click.FloatRange(min=0),
    default=0.1,
    show_default=True,
    help="Pauza medzi firmami v sekundách.",
)
@with_appcontext
def enrich_financials_command(
    max_companies: int,
    include_existing: bool,
    delay: float,
) -> None:
    """Doplní posledné verejné finančné údaje z RÚZ."""
    click.echo("Dopĺňam finančné údaje z RÚZ...")
    result = enrich_company_financials(
        max_companies=max_companies,
        include_existing=include_existing,
        delay_seconds=delay,
    )
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@click.command("backfill-rpo-fields")
@with_appcontext
def backfill_rpo_fields_command() -> None:
    """Doplní nové polia z už uložených RPO záznamov."""
    result = backfill_rpo_company_fields()
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@click.command("enrich-websites")
@click.option(
    "--max-companies",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    help="Počet firiem, ktorých web sa analyzuje cez AI.",
)
@click.option(
    "--include-analyzed",
    is_flag=True,
    default=False,
    help="Analyzuje aj firmy, ktoré už majú webový profil.",
)
@click.option(
    "--ico",
    default=None,
    help="Analyzuje konkrétnu firmu podľa IČO.",
)
@with_appcontext
def enrich_websites_command(
    max_companies: int,
    include_analyzed: bool,
    ico: str | None,
) -> None:
    """Doplní obchodný profil firmy z relevantných stránok jej webu."""
    click.echo("Analyzujem firemné weby cez AI...")
    result = enrich_company_websites(
        max_companies=max_companies,
        include_analyzed=include_analyzed,
        ico=ico,
    )
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
