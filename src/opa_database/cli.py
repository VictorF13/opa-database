"""Click entrypoint for running bronze-layer ingestion."""

import click

from opa_database.adapters import avl


@click.group()
def cli() -> None:
    """OPA Database ingestion commands."""


@cli.command()
@click.argument("source", type=click.Choice(["avl"]))
@click.option("--year", type=int, required=True)
@click.option("--month", type=int, required=True)
def ingest(source: str, year: int, month: int) -> None:
    """Ingest a raw SOURCE for a given year/month into the bronze layer."""
    written = avl.ingest(year, month) if source == "avl" else []
    for path in written:
        click.echo(path)
    click.echo(f"Wrote {len(written)} bronze partition(s).")
