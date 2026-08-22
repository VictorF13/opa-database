"""Generic writer for loading bronze data into the silver PostgreSQL+PostGIS layer."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, LiteralString, NamedTuple

import psycopg
from psycopg import sql

from opa_database.config import settings

if TYPE_CHECKING:
    import datetime
    from collections.abc import Sequence

    import polars as pl


def get_connection() -> psycopg.Connection:
    """Open a connection to the silver database.

    Returns:
        psycopg.Connection: An open connection to the silver database.

    """
    return psycopg.connect(settings.db_dsn)


def ensure_schema(conn: psycopg.Connection) -> None:
    """Create the `silver` schema if it doesn't already exist.

    Args:
        conn (psycopg.Connection): An open connection to the silver
            database.

    """
    conn.execute("CREATE SCHEMA IF NOT EXISTS silver;")


def monthly_partition_name(table: str, year: int, month: int) -> str:
    """Build a monthly silver partition's bare table name.

    Args:
        table (str): Bare table name, e.g. "avl_pings".
        year (int): Calendar year.
        month (int): Calendar month.

    Returns:
        str: e.g. "avl_pings_y2023m11".

    """
    return f"{table}_y{year}m{month:02d}"


def daily_partition_name(table: str, date: datetime.date) -> str:
    """Build a daily silver partition's bare table name.

    Args:
        table (str): Bare table name, e.g. "gtfs_stop_times".
        date (datetime.date): The day this partition covers.

    Returns:
        str: e.g. "gtfs_stop_times_d20240328".

    """
    return f"{table}_d{date:%Y%m%d}"


class IndexSpec(NamedTuple):
    """One index to create on a silver partition after it's bulk-loaded.

    Attributes:
        suffix (str): Appended to the partition's own table name to form
            the index name (e.g. partition "avl_pings_y2023m11" + suffix
            "geom_idx" -> index "avl_pings_y2023m11_geom_idx").
        unique (bool): Whether to create a `UNIQUE` index.
        definition (LiteralString): The literal index definition text
            following the target table name in a `CREATE INDEX`
            statement, e.g. "USING GIST (geom)" or
            "(event_id) WHERE event_id != '0'". Must be a hardcoded
            literal, not built from dynamic/user input.

    """

    suffix: str
    unique: bool
    definition: LiteralString


def replace_period(
    conn: psycopg.Connection,
    table: str,
    partition: str,
    df: pl.DataFrame,
    *,
    partition_start: datetime.date | datetime.datetime,
    partition_end: datetime.date | datetime.datetime,
    parent_ddl: LiteralString,
    indexes: Sequence[IndexSpec] = (),
) -> None:
    """Idempotently load one period's worth of rows into a silver partition.

    Bootstraps the partitioned parent table (`parent_ddl`, a `CREATE
    TABLE IF NOT EXISTS ... PARTITION BY RANGE` statement), then drops
    and recreates the target partition and bulk-loads `df` into it, all
    in one transaction — so re-running a load for the same period
    reflects a re-run rather than accumulating duplicates.

    Dropping the whole partition (rather than `DELETE`ing its rows) also
    removes its indexes in the same metadata-only operation, so the
    fresh partition is bulk-loaded with no indexes present and only gets
    them built afterward. Incremental per-row index maintenance during a
    multi-million-row `COPY` (especially GiST) is dramatically slower
    than one batch build afterward (Postgres's own documented
    recommendation for bulk loads) — this preserves that, but scoped to
    just the one partition being (re)loaded, so cost scales with one
    period's size, never the table's total accumulated history.

    Args:
        conn (psycopg.Connection): An open connection to the silver
            database.
        table (str): Fully-qualified parent table name (e.g.
            "silver.avl_pings").
        partition (str): Bare name of the partition to (re)load (e.g.
            "avl_pings_y2023m11"), resolved as `silver.<partition>`.
        df (pl.DataFrame): The data to load, with columns matching the
            target table's insertable (non-generated) columns, in order.
        partition_start (datetime.date | datetime.datetime): Inclusive
            start of the partition's range.
        partition_end (datetime.date | datetime.datetime): Exclusive end
            of the partition's range.
        parent_ddl (LiteralString): `CREATE TABLE IF NOT EXISTS ...
            PARTITION BY RANGE (...)` statement for the parent, run
            before loading so it exists on first use. Must be a
            hardcoded literal (not built from dynamic/user input).
        indexes (Sequence[IndexSpec]): Indexes to create on the
            partition after it's loaded.

    """
    ensure_schema(conn)
    schema = table.split(".", maxsplit=1)[0]
    qualified_parent = sql.Identifier(*table.split("."))
    qualified_partition = sql.Identifier(schema, partition)
    columns = sql.SQL(", ").join(sql.Identifier(c) for c in df.columns)

    with conn.transaction():
        conn.execute(parent_ddl)

        # Dropping the partition (if this period was already loaded)
        # removes its rows AND its indexes in one metadata operation,
        # and never touches any other partition regardless of table size.
        conn.execute(
            sql.SQL("DROP TABLE IF EXISTS {partition}").format(
                partition=qualified_partition
            )
        )
        conn.execute(
            sql.SQL(
                "CREATE TABLE {partition} PARTITION OF {parent} "
                "FOR VALUES FROM ({start}) TO ({end})"
            ).format(
                partition=qualified_partition,
                parent=qualified_parent,
                start=sql.Literal(partition_start),
                end=sql.Literal(partition_end),
            )
        )

        copy_query = sql.SQL(
            "COPY {partition} ({columns}) FROM STDIN (FORMAT CSV)"
        ).format(
            partition=qualified_partition,
            columns=columns,
        )
        # Bulk CSV write beats row-by-row copy.write_row() by orders of
        # magnitude: Polars serializes the whole frame in one vectorized
        # pass instead of a 100M+-iteration Python loop. Writing to a
        # BytesIO buffer (rather than a str, then .encode()) avoids holding
        # two full in-memory copies of a potentially huge CSV blob.
        buffer = io.BytesIO()
        df.write_csv(buffer, include_header=False)
        with conn.cursor().copy(copy_query) as copy:
            copy.write(buffer.getvalue())

        for spec in indexes:
            # CREATE INDEX names are always resolved within the target
            # table's own schema, and (unlike DROP INDEX) cannot be
            # schema-qualified in the statement itself.
            index_name = sql.Identifier(f"{partition}_{spec.suffix}")
            conn.execute(
                sql.SQL(
                    "CREATE {unique}INDEX {name} ON {partition} {definition}"
                ).format(
                    unique=sql.SQL("UNIQUE ") if spec.unique else sql.SQL(""),
                    name=index_name,
                    partition=qualified_partition,
                    definition=sql.SQL(spec.definition),
                )
            )
