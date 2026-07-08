"""Click entrypoint for running bronze-layer ingestion."""

import click

from opa_database.adapters import avl, gtfs

_ADAPTERS = {
    "avl": avl,
    "gtfs": gtfs,
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
