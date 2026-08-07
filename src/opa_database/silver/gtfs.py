"""Silver loader for the GTFS source: bronze parquet to PostgreSQL+PostGIS.

Bronze partitions every GTFS table by the feed *export* date, not a
calendar period — a schedule snapshot stays in effect until the next
export supersedes it, so there's no "November's schedule" the way there's
"November's ridership." Same resolution as AFC's dump_date-vs-service_date
split: `replace_period()`'s period here is the export date itself
(`feed_version_date`), one snapshot fully replacing itself if reloaded.
Every table is partitioned by day to match — each export IS one complete,
self-contained snapshot, its true natural unit.
`shapes`/`stops` get a generated PostGIS point per row; aggregating shape
points into a `LINESTRING` per route is left for a downstream consumer,
same as the AFC trips/boardings normalization.
"""

from __future__ import annotations

import datetime
from typing import LiteralString

import polars as pl

from opa_database.config import settings
from opa_database.loaders.silver import (
    IndexSpec,
    daily_partition_name,
    get_connection,
    replace_period,
)

_PARENT_DDL: dict[str, LiteralString] = {
    "agency": """
        CREATE TABLE IF NOT EXISTS silver.gtfs_agency (
            feed_version_date date NOT NULL,
            agency_id text,
            agency_name text NOT NULL,
            agency_url text NOT NULL,
            agency_timezone text NOT NULL,
            agency_lang text,
            agency_phone text,
            agency_fare_url text
        ) PARTITION BY RANGE (feed_version_date);
    """,
    "calendar": """
        CREATE TABLE IF NOT EXISTS silver.gtfs_calendar (
            feed_version_date date NOT NULL,
            service_id text NOT NULL,
            monday integer NOT NULL,
            tuesday integer NOT NULL,
            wednesday integer NOT NULL,
            thursday integer NOT NULL,
            friday integer NOT NULL,
            saturday integer NOT NULL,
            sunday integer NOT NULL,
            start_date date NOT NULL,
            end_date date NOT NULL
        ) PARTITION BY RANGE (feed_version_date);
    """,
    "calendar_dates": """
        CREATE TABLE IF NOT EXISTS silver.gtfs_calendar_dates (
            feed_version_date date NOT NULL,
            service_id text NOT NULL,
            date date NOT NULL,
            exception_type integer NOT NULL,
            copied_from_feed_version_date date
        ) PARTITION BY RANGE (feed_version_date);
    """,
    "fare_attributes": """
        CREATE TABLE IF NOT EXISTS silver.gtfs_fare_attributes (
            feed_version_date date NOT NULL,
            fare_id text NOT NULL,
            price double precision NOT NULL,
            currency_type text NOT NULL,
            payment_method integer NOT NULL,
            transfers integer,
            transfer_duration integer
        ) PARTITION BY RANGE (feed_version_date);
    """,
    "fare_rules": """
        CREATE TABLE IF NOT EXISTS silver.gtfs_fare_rules (
            feed_version_date date NOT NULL,
            fare_id text NOT NULL,
            route_id text,
            origin_id text,
            destination_id text,
            contains_id text
        ) PARTITION BY RANGE (feed_version_date);
    """,
    "routes": """
        CREATE TABLE IF NOT EXISTS silver.gtfs_routes (
            feed_version_date date NOT NULL,
            route_id text NOT NULL,
            agency_id text,
            route_short_name text,
            route_long_name text,
            route_desc text,
            route_type integer NOT NULL,
            route_url text,
            route_color text,
            route_text_color text
        ) PARTITION BY RANGE (feed_version_date);
    """,
    "shapes": """
        CREATE TABLE IF NOT EXISTS silver.gtfs_shapes (
            feed_version_date date NOT NULL,
            shape_id text NOT NULL,
            shape_pt_lat double precision NOT NULL,
            shape_pt_lon double precision NOT NULL,
            shape_pt_sequence integer NOT NULL,
            shape_dist_traveled double precision,
            geom geometry(Point, 4326)
                GENERATED ALWAYS AS
                (ST_SetSRID(ST_MakePoint(shape_pt_lon, shape_pt_lat), 4326)) STORED
        ) PARTITION BY RANGE (feed_version_date);
    """,
    "stops": """
        CREATE TABLE IF NOT EXISTS silver.gtfs_stops (
            feed_version_date date NOT NULL,
            stop_id text NOT NULL,
            stop_code text,
            stop_name text NOT NULL,
            stop_desc text,
            stop_lat double precision NOT NULL,
            stop_lon double precision NOT NULL,
            zone_id text,
            stop_url text,
            location_type integer,
            parent_station text,
            stop_timezone text,
            wheelchair_boarding integer,
            geom geometry(Point, 4326)
                GENERATED ALWAYS AS
                (ST_SetSRID(ST_MakePoint(stop_lon, stop_lat), 4326)) STORED
        ) PARTITION BY RANGE (feed_version_date);
    """,
    "stop_times": """
        CREATE TABLE IF NOT EXISTS silver.gtfs_stop_times (
            feed_version_date date NOT NULL,
            trip_id text NOT NULL,
            arrival_time text,
            departure_time text,
            stop_id text NOT NULL,
            stop_sequence integer NOT NULL,
            stop_headsign text,
            pickup_type integer,
            drop_off_type integer,
            shape_dist_traveled double precision,
            copied_from_feed_version_date date
        ) PARTITION BY RANGE (feed_version_date);
    """,
    "trips": """
        CREATE TABLE IF NOT EXISTS silver.gtfs_trips (
            feed_version_date date NOT NULL,
            route_id text NOT NULL,
            service_id text NOT NULL,
            trip_id text NOT NULL,
            trip_headsign text,
            trip_short_name text,
            direction_id integer,
            block_id text,
            shape_id text,
            wheelchair_accessible integer
        ) PARTITION BY RANGE (feed_version_date);
    """,
}

# Created on each partition after it's bulk-loaded, rather than
# incrementally maintained per-row during COPY (see replace_period's
# docstring).
_INDEXES: dict[str, tuple[IndexSpec, ...]] = {
    "agency": (IndexSpec("fvd_idx", unique=False, definition="(feed_version_date)"),),
    "calendar": (
        IndexSpec("fvd_idx", unique=False, definition="(feed_version_date)"),
        IndexSpec("service_id_idx", unique=False, definition="(service_id)"),
    ),
    "calendar_dates": (
        IndexSpec("fvd_idx", unique=False, definition="(feed_version_date)"),
        IndexSpec("service_id_idx", unique=False, definition="(service_id)"),
    ),
    "fare_attributes": (
        IndexSpec("fvd_idx", unique=False, definition="(feed_version_date)"),
    ),
    "fare_rules": (
        IndexSpec("fvd_idx", unique=False, definition="(feed_version_date)"),
        IndexSpec("fare_id_idx", unique=False, definition="(fare_id)"),
    ),
    "routes": (
        IndexSpec("fvd_idx", unique=False, definition="(feed_version_date)"),
        IndexSpec("route_id_idx", unique=False, definition="(route_id)"),
    ),
    "shapes": (
        IndexSpec("fvd_idx", unique=False, definition="(feed_version_date)"),
        IndexSpec("shape_id_idx", unique=False, definition="(shape_id)"),
        IndexSpec("geom_idx", unique=False, definition="USING GIST (geom)"),
    ),
    "stops": (
        IndexSpec("fvd_idx", unique=False, definition="(feed_version_date)"),
        IndexSpec("stop_id_idx", unique=False, definition="(stop_id)"),
        IndexSpec("geom_idx", unique=False, definition="USING GIST (geom)"),
    ),
    "stop_times": (
        IndexSpec("fvd_idx", unique=False, definition="(feed_version_date)"),
        IndexSpec("trip_id_idx", unique=False, definition="(trip_id)"),
    ),
    "trips": (
        IndexSpec("fvd_idx", unique=False, definition="(feed_version_date)"),
        IndexSpec("trip_id_idx", unique=False, definition="(trip_id)"),
        IndexSpec("route_id_idx", unique=False, definition="(route_id)"),
    ),
}

# Column order matches each bronze contract (contracts/gtfs.py), minus the
# generated geom columns.
_COLUMNS: dict[str, tuple[str, ...]] = {
    "agency": (
        "agency_id",
        "agency_name",
        "agency_url",
        "agency_timezone",
        "agency_lang",
        "agency_phone",
        "agency_fare_url",
    ),
    "calendar": (
        "service_id",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "start_date",
        "end_date",
    ),
    "calendar_dates": (
        "service_id",
        "date",
        "exception_type",
        "copied_from_feed_version_date",
    ),
    "fare_attributes": (
        "fare_id",
        "price",
        "currency_type",
        "payment_method",
        "transfers",
        "transfer_duration",
    ),
    "fare_rules": ("fare_id", "route_id", "origin_id", "destination_id", "contains_id"),
    "routes": (
        "route_id",
        "agency_id",
        "route_short_name",
        "route_long_name",
        "route_desc",
        "route_type",
        "route_url",
        "route_color",
        "route_text_color",
    ),
    "shapes": (
        "shape_id",
        "shape_pt_lat",
        "shape_pt_lon",
        "shape_pt_sequence",
        "shape_dist_traveled",
    ),
    "stops": (
        "stop_id",
        "stop_code",
        "stop_name",
        "stop_desc",
        "stop_lat",
        "stop_lon",
        "zone_id",
        "stop_url",
        "location_type",
        "parent_station",
        "stop_timezone",
        "wheelchair_boarding",
    ),
    "stop_times": (
        "trip_id",
        "arrival_time",
        "departure_time",
        "stop_id",
        "stop_sequence",
        "stop_headsign",
        "pickup_type",
        "drop_off_type",
        "shape_dist_traveled",
        "copied_from_feed_version_date",
    ),
    "trips": (
        "route_id",
        "service_id",
        "trip_id",
        "trip_headsign",
        "trip_short_name",
        "direction_id",
        "block_id",
        "shape_id",
        "wheelchair_accessible",
    ),
}

_TABLES = tuple(_PARENT_DDL)


def _find_snapshot_days(year: int, month: int) -> list[int]:
    """List the export days present in bronze for a year/month.

    Every GTFS table shares the same set of snapshot days by construction
    (one bronze adapter run writes all 10 tables for the same export
    date), so any single table's directory listing is representative.
    """
    month_dir = (
        settings.bronze_root / "gtfs" / "routes" / f"year={year}" / f"month={month}"
    )
    if not month_dir.exists():
        return []
    return sorted(
        int(entry.name.removeprefix("day="))
        for entry in month_dir.iterdir()
        if entry.is_dir()
    )


def _read_table(table: str, year: int, month: int, day: int) -> pl.DataFrame:
    path = (
        settings.bronze_root
        / "gtfs"
        / table
        / f"year={year}"
        / f"month={month}"
        / f"day={day}"
        / "data.parquet"
    )
    feed_version_date = datetime.date(year, month, day)
    return (
        pl.scan_parquet(path)
        .with_columns(pl.lit(feed_version_date).alias("feed_version_date"))
        .select("feed_version_date", *_COLUMNS[table])
        .collect()
    )


def load(year: int, month: int) -> None:
    """Load bronze GTFS snapshot(s) for a year/month into the silver layer.

    A month can contain zero, one, or several feed exports (Nov 2023 had
    two). Every table is loaded once per export day found, keyed by that
    day as its own single-day `feed_version_date` partition.

    Args:
        year (int): Calendar year to load.
        month (int): Calendar month to load.

    """
    days = _find_snapshot_days(year, month)
    with get_connection() as conn:
        for day in days:
            date = datetime.date(year, month, day)
            for table in _TABLES:
                df = _read_table(table, year, month, day)
                partition = daily_partition_name(f"gtfs_{table}", date)
                replace_period(
                    conn,
                    f"silver.gtfs_{table}",
                    partition,
                    df,
                    partition_start=date,
                    partition_end=date + datetime.timedelta(days=1),
                    parent_ddl=_PARENT_DDL[table],
                    indexes=_INDEXES[table],
                )
