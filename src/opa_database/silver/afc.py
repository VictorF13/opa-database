"""Silver loader for the AFC/bilhetagem source: bronze parquet to PostgreSQL+PostGIS."""

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

_TABLE = "silver.afc_boardings"
_DECEMBER = 12

# AFC's raw timestamps are naive Fortaleza local time (UTC-3, no DST since
# 2008), not UTC. Localizing to the IANA zone and converting sidesteps the
# sign-flip bug an earlier (pre-opa-database) pipeline had with a
# hardcoded +/-3h magic number.
_LOCAL_TZ = "America/Fortaleza"
_LOCAL_TIMESTAMP_COLUMNS = (
    "line_opened_at",
    "line_closed_at",
    "trip_opened_at",
    "trip_closed_at",
    "boarding_at",
)

# geom is GENERATED, not inserted directly (see silver/avl.py for the same
# pattern): Postgres computes it from latitude/longitude on write. It's
# null whenever either input is, matching bronze's ~14% missing rate.
# Partitioned by month, matching this loader's own whole-month load calls
# (see loaders/silver.py::replace_period).
_PARENT_DDL = """
CREATE TABLE IF NOT EXISTS silver.afc_boardings (
    dump_date date NOT NULL,
    service_date date NOT NULL,
    company_code text NOT NULL,
    company_modality integer NOT NULL,
    category_type integer NOT NULL,
    vehicle_number text NOT NULL,
    validator_id text,
    line_number text NOT NULL,
    line_shift integer NOT NULL,
    line_operator_number text NOT NULL,
    line_fare_table integer NOT NULL,
    line_opened_at timestamptz NOT NULL,
    line_closed_at timestamptz NOT NULL,
    trip_opened_at timestamptz NOT NULL,
    trip_closed_at timestamptz NOT NULL,
    turnstile_start integer NOT NULL,
    turnstile_end integer NOT NULL,
    direction integer NOT NULL,
    stop_open text NOT NULL,
    stop_close text NOT NULL,
    boarding_at timestamptz NOT NULL,
    integration_bum integer NOT NULL,
    integration_type integer NOT NULL,
    event_id text NOT NULL,
    sigben integer NOT NULL,
    passenger_type integer NOT NULL,
    card_id text NOT NULL,
    fare_paid double precision NOT NULL,
    subsidy_value double precision NOT NULL,
    metro_transfer_value double precision NOT NULL,
    latitude double precision,
    longitude double precision,
    geom geometry(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)) STORED
) PARTITION BY RANGE (dump_date);
"""

# Created on each partition after it's bulk-loaded (see replace_period's
# docstring). event_id is a UNIQUE index, not just a plain one: non-zero
# event_ids were verified globally unique across the whole table (zero
# overlap across all 435 day-pairs in November 2023) back when this was a
# single unpartitioned table. Now that the index is per-partition
# (per-month), it only enforces that within one month — a duplicate
# event_id landing in two different months would no longer be caught.
# Accepted tradeoff: Postgres has no native way to enforce true
# cross-partition uniqueness on a non-partition-key column, and the
# empirical finding backing this index still stands as evidence about the
# real data, not just about the guardrail. "0" is excluded (a partial
# index): it's a sentinel the raw feed uses for passenger records with no
# real transaction reference (e.g. fare-exempt boardings), not a real id,
# and it legitimately repeats (~2% of rows in a sampled day).
_INDEXES = (
    IndexSpec("dump_date_idx", unique=False, definition="(dump_date)"),
    IndexSpec("service_date_idx", unique=False, definition="(service_date)"),
    IndexSpec(
        "event_id_key", unique=True, definition="(event_id) WHERE event_id != '0'"
    ),
    IndexSpec("geom_idx", unique=False, definition="USING GIST (geom)"),
)

_COLUMNS = (
    "dump_date",
    "service_date",
    "company_code",
    "company_modality",
    "category_type",
    "vehicle_number",
    "validator_id",
    "line_number",
    "line_shift",
    "line_operator_number",
    "line_fare_table",
    "line_opened_at",
    "line_closed_at",
    "trip_opened_at",
    "trip_closed_at",
    "turnstile_start",
    "turnstile_end",
    "direction",
    "stop_open",
    "stop_close",
    "boarding_at",
    "integration_bum",
    "integration_type",
    "event_id",
    "sigben",
    "passenger_type",
    "card_id",
    "fare_paid",
    "subsidy_value",
    "metro_transfer_value",
    "latitude",
    "longitude",
)


def _period_bounds(year: int, month: int) -> tuple[datetime.date, datetime.date]:
    start = datetime.date(year, month, 1)
    end = (
        datetime.date(year + 1, 1, 1)
        if month == _DECEMBER
        else datetime.date(year, month + 1, 1)
    )
    return start, end


def load(year: int, month: int) -> None:
    """Load one month of bronze AFC dumps into the silver layer.

    "Period" here is the dump file's own date (matching bronze's
    partitioning), not `service_date` — a single dump can carry events for
    service dates going back weeks, so there's no cheap way to select "all
    of November's real ridership" without scanning every dump ever
    ingested. `service_date` stays as a queryable column instead; loading
    "November" means "reprocess November's dumps," not "get November's
    ridership."

    Args:
        year (int): Calendar year to load.
        month (int): Calendar month to load.

    """
    glob = (
        settings.bronze_root
        / "afc"
        / f"year={year}"
        / f"month={month}"
        / "day=*"
        / "data.parquet"
    )
    local_to_utc = [
        pl.col(c).dt.replace_time_zone(_LOCAL_TZ).dt.convert_time_zone("UTC")
        for c in _LOCAL_TIMESTAMP_COLUMNS
    ]
    df = (
        pl.scan_parquet(glob, hive_partitioning=True)
        .with_columns(
            pl.date(pl.col("year"), pl.col("month"), pl.col("day")).alias("dump_date"),
            *local_to_utc,
        )
        .select(_COLUMNS)
        .collect()
    )

    start, end = _period_bounds(year, month)
    partition = monthly_partition_name("afc_boardings", year, month)
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
