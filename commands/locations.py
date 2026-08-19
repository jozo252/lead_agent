from pathlib import Path

import click
from flask.cli import with_appcontext

from extensions import db
from services.postal_locations import (
    download_geonames_postal_rows,
    import_postal_locations,
    postal_rows_from_zip,
)


@click.command("import-postal-locations")
@click.option(
    "--file",
    "source_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Voliteľný lokálny GeoNames SK.zip namiesto stiahnutia.",
)
@with_appcontext
def import_postal_locations_command(source_file):
    """Načíta bezplatné PSČ a súradnice pre radius filtrov."""
    try:
        if source_file:
            rows = postal_rows_from_zip(source_file.read_bytes())
        else:
            rows = download_geonames_postal_rows()

        summary = import_postal_locations(rows)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        raise click.ClickException(f"Import poštových lokalít zlyhal: {exc}") from exc

    click.echo(
        "Poštové lokality: "
        f"{summary['inserted']} nových, "
        f"{summary['updated']} obnovených, "
        f"{summary['total']} spracovaných. "
        "Zdroj: GeoNames, CC BY 4.0."
    )
