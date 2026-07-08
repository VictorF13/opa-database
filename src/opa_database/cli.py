"""Click entrypoint for running bronze-layer ingestion and silver-layer loads."""

import click

from opa_database.adapters import afc, avl, gtfs, vehicle_dictionary
from opa_database.silver import afc as silver_afc
from opa_database.silver import avl as silver_avl

_ADAPTERS = {
    "afc": afc,
    "avl": avl,
    "gtfs": gtfs,
}

_REFERENCE_ADAPTERS = {
    "vehicle_dictionary": vehicle_dictionary,
}

_SILVER_LOADERS = {
    "afc": silver_afc,
    "avl": silver_avl,
}


@click.group()
def cli() -> None:
    """OPA Database ingestion commands."""


@cli.command()
@click.argument("source", type=click.Choice(sorted(_ADAPTERS)))
@click.option("--year", type=int, required=True)
@click.option("--month", type=int, required=True)
def ingest(source: str, year: int, month: int) -> None:
    """Ingest a raw SOURCE for a given year/month into the bronze layer."""
    written = _ADAPTERS[source].ingest(year, month)
    for path in written:
        click.echo(path)
    click.echo(f"Wrote {len(written)} bronze partition(s).")


@cli.command("ingest-reference")
@click.argument("source", type=click.Choice(sorted(_REFERENCE_ADAPTERS)))
def ingest_reference(source: str) -> None:
    """Snapshot a reference SOURCE into the bronze layer."""
    path = _REFERENCE_ADAPTERS[source].ingest()
    click.echo(path)


@cli.command("load-silver")
@click.argument("source", type=click.Choice(sorted(_SILVER_LOADERS)))
@click.option("--year", type=int, required=True)
@click.option("--month", type=int, required=True)
def load_silver(source: str, year: int, month: int) -> None:
    """Load bronze SOURCE for a given year/month into the silver layer."""
    _SILVER_LOADERS[source].load(year, month)
    click.echo(f"Loaded silver.{source} for {year}-{month:02d}.")
