import json
import signal
import threading

import click
from flask.cli import with_appcontext

from services.rpo_sync import (
    backfill_rpo_company_fields,
    enrich_company_contacts,
    import_rpo_companies_batch,
    import_rpo_sole_traders_batch,
    import_rpo_sole_traders_export,
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
@click.option(
    "--full",
    "full_sync",
    is_flag=True,
    default=False,
    help=(
        "Ignoruje lokálny checkpoint synchronizačného API. Na kompletný "
        "historický export použite import-rpo-sole-traders."
    ),
)
@with_appcontext
def sync_rpo_command(
    max_records: int | None,
    only_ids: bool,
    commit_every: int,
    delay: float,
    restart: bool,
    full_sync: bool,
) -> None:
    """
    Synchronizuje firmy a živnostníkov z RPO V2.
    """

    click.echo("Spúšťam RPO2 synchronizáciu...")

    try:
        result = sync_rpo(
            max_records=max_records,
            only_ids=only_ids,
            commit_every=commit_every,
            delay_seconds=delay,
            resume=not restart,
            full_sync=full_sync,
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


@click.command("import-rpo-sole-traders")
@click.option(
    "--batch-date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    required=True,
    help="Dátum mesačnej inicializačnej dávky RPO.",
)
@click.option(
    "--file-number",
    type=click.IntRange(min=1, max=999),
    required=True,
    help="Poradové číslo súboru inicializačnej dávky.",
)
@click.option(
    "--max-records",
    type=click.IntRange(min=1),
    default=10_000,
    show_default=True,
    help=(
        "Maximálny počet aktívnych živnostníkov z elektro, stavebníctva "
        "a príbuzných technických činností na import."
    ),
)
@click.option(
    "--commit-every",
    type=click.IntRange(min=1),
    default=100,
    show_default=True,
    help="Počet živnostníkov medzi databázovými commitmi.",
)
@with_appcontext
def import_rpo_sole_traders_command(
    batch_date,
    file_number: int,
    max_records: int,
    commit_every: int,
) -> None:
    """Import target-sector active sole traders from an RPO export."""

    click.echo(
        "Importujem cielených elektro a stavebných živnostníkov "
        "z oficiálneho exportu RPO..."
    )
    try:
        result = import_rpo_sole_traders_export(
            batch_date=batch_date.date().isoformat(),
            file_number=file_number,
            max_records=max_records,
            commit_every=commit_every,
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


@click.command("import-rpo-sole-traders-batch")
@click.option(
    "--batch-date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    required=True,
    help="Dátum mesačnej inicializačnej dávky RPO.",
)
@click.option(
    "--first-file",
    type=click.IntRange(min=1, max=999),
    default=1,
    show_default=True,
    help="Prvý súbor dávky.",
)
@click.option(
    "--last-file",
    type=click.IntRange(min=1, max=999),
    default=23,
    show_default=True,
    help="Posledný súbor dávky.",
)
@click.option(
    "--commit-every",
    type=click.IntRange(min=1),
    default=100,
    show_default=True,
    help="Počet skontrolovaných záznamov medzi checkpointmi.",
)
@click.option(
    "--max-records",
    type=click.IntRange(min=1),
    default=None,
    help="Zastaví dávku po tomto počte relevantných záznamov.",
)
@click.option(
    "--restart",
    is_flag=True,
    default=False,
    help="Zahodí checkpoint a začne znovu od prvého súboru.",
)
@with_appcontext
def import_rpo_sole_traders_batch_command(
    batch_date,
    first_file: int,
    last_file: int,
    commit_every: int,
    max_records: int | None,
    restart: bool,
) -> None:
    """Import a resumable target-sector RPO batch."""

    stop_event = threading.Event()

    def request_stop(_signum, _frame):
        stop_event.set()

    previous_handlers = {}
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signal_number] = signal.getsignal(signal_number)
        signal.signal(signal_number, request_stop)

    click.echo(
        "Spúšťam obnoviteľný import. Ctrl+C alebo SIGTERM uloží "
        "aktuálny checkpoint."
    )
    try:
        result = import_rpo_sole_traders_batch(
            batch_date=batch_date.date().isoformat(),
            first_file=first_file,
            last_file=last_file,
            commit_every=commit_every,
            max_records=max_records,
            resume=not restart,
            should_stop=stop_event.is_set,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)

    click.echo(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@click.command("import-rpo-companies-batch")
@click.option(
    "--batch-date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    required=True,
    help="Dátum mesačnej inicializačnej dávky RPO.",
)
@click.option(
    "--first-file",
    type=click.IntRange(min=1, max=999),
    default=1,
    show_default=True,
    help="Prvý súbor dávky.",
)
@click.option(
    "--last-file",
    type=click.IntRange(min=1, max=999),
    default=23,
    show_default=True,
    help="Posledný súbor dávky.",
)
@click.option(
    "--commit-every",
    type=click.IntRange(min=1),
    default=100,
    show_default=True,
    help="Počet skontrolovaných záznamov medzi checkpointmi.",
)
@click.option(
    "--max-records",
    type=click.IntRange(min=1),
    default=None,
    help="Zastaví dávku po tomto kumulatívnom počte aktívnych s. r. o.",
)
@click.option(
    "--restart",
    is_flag=True,
    default=False,
    help="Zahodí firemný exportový checkpoint a začne od prvého súboru.",
)
@with_appcontext
def import_rpo_companies_batch_command(
    batch_date,
    first_file: int,
    last_file: int,
    commit_every: int,
    max_records: int | None,
    restart: bool,
) -> None:
    """Import active s.r.o. companies from a resumable RPO batch."""

    stop_event = threading.Event()

    def request_stop(_signum, _frame):
        stop_event.set()

    previous_handlers = {}
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signal_number] = signal.getsignal(signal_number)
        signal.signal(signal_number, request_stop)

    click.echo(
        "Spúšťam obnoviteľný import aktívnych s. r. o. z RPO. "
        "Ctrl+C alebo SIGTERM uloží aktuálny checkpoint."
    )
    try:
        result = import_rpo_companies_batch(
            batch_date=batch_date.date().isoformat(),
            first_file=first_file,
            last_file=last_file,
            commit_every=commit_every,
            max_records=max_records,
            resume=not restart,
            should_stop=stop_event.is_set,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)

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
