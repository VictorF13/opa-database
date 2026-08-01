"""Silver loader for the AVL/GPS source: bronze parquet to PostgreSQL+PostGIS."""

from __future__ import annotations

import datetime

import polars as pl

from opa_database.config import settings
from opa_database.loaders.silver import (
    IndexSpec,
    get_connection,
    monthly_partition_name,
    replace_period,
)

_TABLE = "silver.avl_pings"

# geom is GENERATED, not inserted directly: Postgres computes it from
# latitude/longitude on write, so it doesn't need to travel through bronze
# or be part of the COPY's column list. Partitioned by month: one
# replace_period() call already loads exactly one month, so each call
# maps to exactly one partition (see loaders/silver.py::replace_period).
_PARENT_DDL = """
CREATE TABLE IF NOT EXISTS silver.avl_pings (
    vehicle_id integer NOT NULL,
    device_id text NOT NULL,
    direction integer NOT NULL,
    odometer bigint NOT NULL,
    route_code integer NOT NULL,
    speed integer NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL,
    metric_timestamp timestamptz NOT NULL,
    geom geometry(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)) STORED
) PARTITION BY RANGE (metric_timestamp);
"""

# Created on each partition after it's bulk-loaded, rather than
# incrementally maintained per-row during COPY (see replace_period's
# docstring).
_INDEXES = (
    IndexSpec("geom_idx", unique=False, definition="USING GIST (geom)"),
    IndexSpec("ts_idx", unique=False, definition="(metric_timestamp)"),
)

_DECEMBER = 12

_COLUMNS = (
    "vehicle_id",
    "device_id",
    "direction",
    "odometer",
    "route_code",
    "speed",
    "latitude",
    "longitude",
    "metric_timestamp",
)


def _period_bounds(
    year: int, month: int
) -> tuple[datetime.datetime, datetime.datetime]:
    start = datetime.datetime(year, month, 1, tzinfo=datetime.UTC)
    end = (
        datetime.datetime(year + 1, 1, 1, tzinfo=datetime.UTC)
        if month == _DECEMBER
        else datetime.datetime(year, month + 1, 1, tzinfo=datetime.UTC)
    )
    return start, end


def load(year: int, month: int) -> None:
    """Load one month of bronze AVL/GPS pings into the silver layer.

    The raw feed's `metric_timestamp` is already UTC (per the raw
    `DADOS_GPS/GPS data fields.txt` dictionary's own "metrictimestamp
    (UTC-0)" label), so this is a relabeling to a proper `timestamptz`,
    not a shift.

    Args:
        year (int): Calendar year to load.
        month (int): Calendar month to load.

    """
    glob = (
        settings.bronze_root
        / "avl"
        / f"year={year}"
        / f"month={month}"
        / "day=*"
        / "data.parquet"
    )
    df = (
        pl.scan_parquet(glob)
        .with_columns(pl.col("metric_timestamp").dt.replace_time_zone("UTC"))
        .select(_COLUMNS)
        .collect()
    )

    start, end = _period_bounds(year, month)
    partition = monthly_partition_name("avl_pings", year, month)
    with get_connection() as conn:
        replace_period(
            conn,
            _TABLE,
            partition,
            df,
            partition_start=start,
            partition_end=end,
            parent_ddl=_PARENT_DDL,
            indexes=_INDEXES,
        )
