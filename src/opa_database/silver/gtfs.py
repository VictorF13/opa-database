"""Silver loader for the GTFS source: bronze parquet to PostgreSQL+PostGIS.

Bronze partitions every GTFS table by the feed *export* date, not a
calendar period — a schedule snapshot stays in effect until the next
export supersedes it, so there's no "November's schedule" the way there's
"November's ridership." Same resolution as AFC's dump_date-vs-service_date
split: `replace_period()`'s period here is the export date itself
(`feed_version_date`), one snapshot fully replacing itself if reloaded.
`shapes`/`stops` get a generated PostGIS point per row; aggregating shape
points into a `LINESTRING` per route is left for a gold/dbt model, same as
the AFC trips/boardings normalization.
"""

from __future__ import annotations

import datetime
from typing import LiteralString

import polars as pl

from opa_database.config import settings
from opa_database.loaders.silver import get_connection, replace_period

_TABLE_DDL: dict[str, LiteralString] = {
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
        );
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
        );
    """,
    "calendar_dates": """
        CREATE TABLE IF NOT EXISTS silver.gtfs_calendar_dates (
            feed_version_date date NOT NULL,
            service_id text NOT NULL,
            date date NOT NULL,
            exception_type integer NOT NULL
        );
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
        );
    """,
    "fare_rules": """
        CREATE TABLE IF NOT EXISTS silver.gtfs_fare_rules (
            feed_version_date date NOT NULL,
            fare_id text NOT NULL,
            route_id text,
            origin_id text,
            destination_id text,
            contains_id text
        );
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
        );
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
        );
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
        );
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
            shape_dist_traveled double precision
        );
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
        );
    """,
}

_INDEXES: dict[str, tuple[tuple[str, LiteralString], ...]] = {
    "agency": (
        (
            "gtfs_agency_fvd_idx",
            "CREATE INDEX gtfs_agency_fvd_idx "
            "ON silver.gtfs_agency (feed_version_date);",
        ),
    ),
    "calendar": (
        (
            "gtfs_calendar_fvd_idx",
            "CREATE INDEX gtfs_calendar_fvd_idx "
            "ON silver.gtfs_calendar (feed_version_date);",
        ),
        (
            "gtfs_calendar_service_id_idx",
            "CREATE INDEX gtfs_calendar_service_id_idx "
            "ON silver.gtfs_calendar (service_id);",
        ),
    ),
    "calendar_dates": (
        (
            "gtfs_calendar_dates_fvd_idx",
            "CREATE INDEX gtfs_calendar_dates_fvd_idx "
            "ON silver.gtfs_calendar_dates (feed_version_date);",
        ),
        (
            "gtfs_calendar_dates_service_id_idx",
            "CREATE INDEX gtfs_calendar_dates_service_id_idx "
            "ON silver.gtfs_calendar_dates (service_id);",
        ),
    ),
    "fare_attributes": (
        (
            "gtfs_fare_attributes_fvd_idx",
            "CREATE INDEX gtfs_fare_attributes_fvd_idx "
            "ON silver.gtfs_fare_attributes (feed_version_date);",
        ),
    ),
    "fare_rules": (
        (
            "gtfs_fare_rules_fvd_idx",
            "CREATE INDEX gtfs_fare_rules_fvd_idx "
            "ON silver.gtfs_fare_rules (feed_version_date);",
        ),
        (
            "gtfs_fare_rules_fare_id_idx",
            "CREATE INDEX gtfs_fare_rules_fare_id_idx "
            "ON silver.gtfs_fare_rules (fare_id);",
        ),
    ),
    "routes": (
        (
            "gtfs_routes_fvd_idx",
            "CREATE INDEX gtfs_routes_fvd_idx "
            "ON silver.gtfs_routes (feed_version_date);",
        ),
        (
            "gtfs_routes_route_id_idx",
            "CREATE INDEX gtfs_routes_route_id_idx ON silver.gtfs_routes (route_id);",
        ),
    ),
    "shapes": (
        (
            "gtfs_shapes_fvd_idx",
            "CREATE INDEX gtfs_shapes_fvd_idx "
            "ON silver.gtfs_shapes (feed_version_date);",
        ),
        (
            "gtfs_shapes_shape_id_idx",
            "CREATE INDEX gtfs_shapes_shape_id_idx ON silver.gtfs_shapes (shape_id);",
        ),
        (
            "gtfs_shapes_geom_idx",
            "CREATE INDEX gtfs_shapes_geom_idx "
            "ON silver.gtfs_shapes USING GIST (geom);",
        ),
    ),
    "stops": (
        (
            "gtfs_stops_fvd_idx",
            "CREATE INDEX gtfs_stops_fvd_idx ON silver.gtfs_stops (feed_version_date);",
        ),
        (
            "gtfs_stops_stop_id_idx",
            "CREATE INDEX gtfs_stops_stop_id_idx ON silver.gtfs_stops (stop_id);",
        ),
        (
            "gtfs_stops_geom_idx",
            "CREATE INDEX gtfs_stops_geom_idx ON silver.gtfs_stops USING GIST (geom);",
        ),
    ),
    "stop_times": (
        (
            "gtfs_stop_times_fvd_idx",
            "CREATE INDEX gtfs_stop_times_fvd_idx "
            "ON silver.gtfs_stop_times (feed_version_date);",
        ),
        (
            "gtfs_stop_times_trip_id_idx",
            "CREATE INDEX gtfs_stop_times_trip_id_idx "
            "ON silver.gtfs_stop_times (trip_id);",
        ),
    ),
    "trips": (
        (
            "gtfs_trips_fvd_idx",
            "CREATE INDEX gtfs_trips_fvd_idx ON silver.gtfs_trips (feed_version_date);",
        ),
        (
            "gtfs_trips_trip_id_idx",
            "CREATE INDEX gtfs_trips_trip_id_idx ON silver.gtfs_trips (trip_id);",
        ),
        (
            "gtfs_trips_route_id_idx",
            "CREATE INDEX gtfs_trips_route_id_idx ON silver.gtfs_trips (route_id);",
        ),
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
    "calendar_dates": ("service_id", "date", "exception_type"),
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

_TABLES = tuple(_TABLE_DDL)


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
    day as its own single-day `feed_version_date` period.

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
                replace_period(
                    conn,
                    f"silver.gtfs_{table}",
                    df,
                    time_column="feed_version_date",
                    start=date,
                    end=date + datetime.timedelta(days=1),
                    table_ddl=_TABLE_DDL[table],
                    indexes=_INDEXES[table],
                )
