"""Click entrypoint for running bronze-layer ingestion and silver-layer loads."""

import datetime
import logging

import click

from opa_database.adapters import afc, avl, gtfs, vehicle_dictionary
from opa_database.diamond import build as diamond_build
from opa_database.gold import build as gold_build
from opa_database.silver import afc as silver_afc
from opa_database.silver import avl as silver_avl
from opa_database.silver import gtfs as silver_gtfs
from opa_database.silver import vehicle_dictionary as silver_vehicle_dictionary

_ADAPTERS = {
    "afc": afc,
    "avl": avl,
    "gtfs": gtfs,
}

# All of these live in the single adapters/vehicle_dictionary.py module
# (see its docstring for why); the CLI just needs a source-name ->
# ingest-function mapping, not a source-name -> module mapping.
_REFERENCE_ADAPTERS = {
    "vehicle_dictionary": vehicle_dictionary.ingest_vehicle,
    "device_dictionary": vehicle_dictionary.ingest_device_dictionary,
    "vehicle_dictionary_legacy": vehicle_dictionary.ingest_legacy,
    "vehicle_dictionary_legacy2": vehicle_dictionary.ingest_legacy2,
    "vehicle_dictionary_2018": vehicle_dictionary.ingest_2018,
    "vehicle_dictionary_antigo": vehicle_dictionary.ingest_antigo,
}

_SILVER_LOADERS = {
    "afc": silver_afc,
    "avl": silver_avl,
    "gtfs": silver_gtfs,
}

# All of these live in the single silver/vehicle_dictionary.py module
# (see its docstring for why); the CLI just needs a source-name ->
# load-function mapping, not a source-name -> module mapping.
_SILVER_REFERENCE_LOADERS = {
    "vehicle_dictionary": silver_vehicle_dictionary.load_vehicle,
    "device_dictionary": silver_vehicle_dictionary.load_device_dictionary,
    "vehicle_dictionary_legacy": silver_vehicle_dictionary.load_legacy,
    "vehicle_dictionary_legacy2": silver_vehicle_dictionary.load_legacy2,
    "vehicle_dictionary_2018": silver_vehicle_dictionary.load_2018,
    "vehicle_dictionary_antigo": silver_vehicle_dictionary.load_antigo,
}


@click.group()
def cli() -> None:
    """OPA Database ingestion commands."""


@cli.command()
@click.argument("source", type=click.Choice(sorted(_ADAPTERS)))
@click.option("--year", type=int, required=True)
@click.option("--month", type=int, required=True)
def ingest(source: str, year: int, month: int) -> None:
    """Ingest a raw SOURCE for a given year/month into the bronze layer.

    Args:
        source (str): Raw source to ingest (`avl`, `afc`, or `gtfs`).
        year (int): Calendar year to ingest.
        month (int): Calendar month to ingest.

    """
    written = _ADAPTERS[source].ingest(year, month)
    for path in written:
        click.echo(path)
    click.echo(f"Wrote {len(written)} bronze partition(s).")


@cli.command("ingest-reference")
@click.argument("source", type=click.Choice(sorted(_REFERENCE_ADAPTERS)))
@click.option(
    "--snapshot-date",
    "snapshot_date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help=(
        "Date to record the snapshot as (YYYY-MM-DD). Required for "
        "device_dictionary, since it has no single current file to "
        "default to; optional for every other reference source, which "
        "default to today."
    ),
)
def ingest_reference(source: str, snapshot_date: datetime.datetime | None) -> None:
    """Snapshot a reference SOURCE into the bronze layer.

    Args:
        source (str): Reference source to snapshot (`vehicle_dictionary`,
            `device_dictionary`, `vehicle_dictionary_legacy`,
            `vehicle_dictionary_legacy2`, `vehicle_dictionary_2018`, or
            `vehicle_dictionary_antigo`).
        snapshot_date (datetime.datetime | None): Date to record the
            snapshot as.

    """
    date = snapshot_date.date() if snapshot_date else None
    path = _REFERENCE_ADAPTERS[source](date)
    click.echo(path)


@cli.command("load-silver")
@click.argument("source", type=click.Choice(sorted(_SILVER_LOADERS)))
@click.option("--year", type=int, required=True)
@click.option("--month", type=int, required=True)
def load_silver(source: str, year: int, month: int) -> None:
    """Load bronze SOURCE for a given year/month into the silver layer.

    Args:
        source (str): Bronze source to load (`avl`, `afc`, or `gtfs`).
        year (int): Calendar year to load.
        month (int): Calendar month to load.

    """
    _SILVER_LOADERS[source].load(year, month)
    click.echo(f"Loaded silver.{source} for {year}-{month:02d}.")


@cli.command("load-silver-reference")
@click.argument("source", type=click.Choice(sorted(_SILVER_REFERENCE_LOADERS)))
@click.option(
    "--snapshot-date",
    "snapshot_date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help=(
        "Bronze snapshot date to load (YYYY-MM-DD). Defaults to the "
        "most recent snapshot found for SOURCE under bronze_root."
    ),
)
def load_silver_reference(source: str, snapshot_date: datetime.datetime | None) -> None:
    """Load a reference SOURCE snapshot into the silver layer.

    Args:
        source (str): Reference source to load (`vehicle_dictionary`,
            `device_dictionary`, `vehicle_dictionary_legacy`,
            `vehicle_dictionary_legacy2`, `vehicle_dictionary_2018`, or
            `vehicle_dictionary_antigo`).
        snapshot_date (datetime.datetime | None): Bronze snapshot date to
            load. Defaults to the most recent snapshot found.

    """
    date = snapshot_date.date() if snapshot_date else None
    table = _SILVER_REFERENCE_LOADERS[source](date)
    click.echo(f"Loaded {table}.")


@cli.command("build-gold")
@click.option("--year", type=int, required=True)
@click.option("--month", type=int, required=True)
def build_gold(year: int, month: int) -> None:
    """Build the gold star schema for a given year/month from scratch.

    Args:
        year (int): Calendar year to build gold for.
        month (int): Calendar month to build gold for.

    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    gold_build.build(year, month)
    click.echo(f"Built gold schema for {year}-{month:02d}.")


@cli.command("build-diamond")
@click.option("--year", type=int, required=True)
@click.option("--month", type=int, required=True)
def build_diamond(year: int, month: int) -> None:
    """Build diamond.stop_arrivals for a given year/month from scratch.

    Args:
        year (int): Calendar year to build diamond for.
        month (int): Calendar month to build diamond for.

    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    diamond_build.build(year, month)
    click.echo(f"Built diamond.stop_arrivals for {year}-{month:02d}.")
