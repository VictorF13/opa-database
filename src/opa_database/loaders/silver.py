"""Generic writer for loading bronze data into the silver PostgreSQL+PostGIS layer."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, LiteralString

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
    return psycopg.connect(settings.silver_dsn)


def ensure_schema(conn: psycopg.Connection) -> None:
    """Create the `silver` schema if it doesn't already exist.

    Args:
        conn (psycopg.Connection): An open connection to the silver
            database.

    """
    conn.execute("CREATE SCHEMA IF NOT EXISTS silver;")


def replace_period(
    conn: psycopg.Connection,
    table: str,
    df: pl.DataFrame,
    *,
    time_column: str,
    start: datetime.date,
    end: datetime.date,
    table_ddl: LiteralString,
    indexes: Sequence[tuple[str, LiteralString]] = (),
) -> None:
    """Idempotently load a period's worth of rows into a silver table.

    Bootstraps the table (`table_ddl`, a `CREATE TABLE IF NOT EXISTS`
    statement), then deletes any existing rows in `[start, end)` and
    bulk-loads `df` in their place, all in one transaction — so re-running a
    load for the same period reflects a re-run rather than accumulating
    duplicates.

    Indexes are dropped before the delete+copy and rebuilt after, rather
    than left in place to be incrementally maintained row-by-row: for a
    GiST spatial index especially, incremental maintenance during a
    multi-million-row COPY is dramatically slower than one batch rebuild
    afterward (this is Postgres's own documented recommendation for bulk
    loads). This does mean every load rebuilds indexes for the *whole*
    table, not just the period being replaced, so cost grows with total
    table size — fine while there's a handful of months of history, but
    worth revisiting (e.g. native partitioning by month) once it isn't.

    Args:
        conn (psycopg.Connection): An open connection to the silver
            database.
        table (str): Fully-qualified table name (e.g. "silver.avl_pings").
        df (pl.DataFrame): The data to load, with columns matching the
            target table's insertable (non-generated) columns, in order.
        time_column (str): Column used to bound the period being replaced.
        start (datetime.date): Inclusive start of the period being
            replaced.
        end (datetime.date): Exclusive end of the period being replaced.
        table_ddl (LiteralString): `CREATE TABLE IF NOT EXISTS` statement
            to run before loading, so the table exists on first use. Must
            be a hardcoded literal (not built from dynamic/user input).
        indexes (Sequence[tuple[str, LiteralString]]): `(index_name,
            CREATE INDEX ...)` pairs to drop before and recreate after the
            load. `CREATE INDEX` statements must be hardcoded literals.

    """
    ensure_schema(conn)
    qualified_table = sql.Identifier(*table.split("."))
    columns = sql.SQL(", ").join(sql.Identifier(c) for c in df.columns)

    with conn.transaction():
        conn.execute(table_ddl)
        for index_name, _ in indexes:
            conn.execute(
                sql.SQL("DROP INDEX IF EXISTS {}").format(
                    sql.Identifier("silver", index_name)
                )
            )

        conn.execute(
            sql.SQL(
                "DELETE FROM {table} WHERE {time_column} >= %s AND {time_column} < %s"
            ).format(
                table=qualified_table,
                time_column=sql.Identifier(time_column),
            ),
            (start, end),
        )

        copy_query = sql.SQL("COPY {table} ({columns}) FROM STDIN (FORMAT CSV)").format(
            table=qualified_table,
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

        for _, create_statement in indexes:
            conn.execute(create_statement)
