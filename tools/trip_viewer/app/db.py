"""Database access for the Trip Viewer app.

Read-only against `gold`/`diamond`. Every function takes an explicit
`trip_id` and queries live - nothing is cached or preloaded across trips.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd
import psycopg

from opa_database.config import settings

if TYPE_CHECKING:
    from collections.abc import Sequence

_META_SQL = """
    SELECT t.trip_id, t.bus_id, t.trip_date, t.trip_start_timestamp,
           t.trip_end_timestamp, r.route_id, r.route_name, rd.direction,
           rdf.feed_version_date
    FROM gold.trips t
    JOIN gold.route_direction_feed rdf
      ON rdf.route_direction_feed_id = t.route_direction_feed_id
    JOIN gold.route_directions rd ON rd.route_direction_id = rdf.route_direction_id
    JOIN gold.routes r ON r.route_id = rd.route_id
    WHERE t.trip_id = %s;
"""

_STOPS_SQL = """
    SELECT rds.stop_sequence, s.stop_id, s.stop_name, s.latitude, s.longitude,
           sa.arrival_time, sa.ping_gap_seconds, sa.confidence
    FROM gold.trips t
    JOIN gold.route_direction_stops rds
      ON rds.route_direction_feed_id = t.route_direction_feed_id
    JOIN gold.stops s ON s.stop_id = rds.stop_id
    LEFT JOIN diamond.stop_arrivals sa
      ON sa.trip_id = t.trip_id AND sa.stop_sequence = rds.stop_sequence
    WHERE t.trip_id = %s
    ORDER BY rds.stop_sequence;
"""

_POSITIONS_SQL = """
    SELECT position_id, ping_at, latitude, longitude, speed, direction
    FROM gold.trip_positions
    WHERE trip_id = %s AND origin = 'avl'
    ORDER BY ping_at;
"""

_SHAPE_SQL = """
    SELECT rdsh.shape_sequence, rdsh.latitude, rdsh.longitude
    FROM gold.trips t
    JOIN gold.route_direction_shapes rdsh USING (route_direction_feed_id)
    WHERE t.trip_id = %s
    ORDER BY rdsh.shape_sequence;
"""

_FARES_SQL = """
    WITH fare_base AS (
        SELECT f.fare_id, f.trip_id, f.card_id, f.passenger_type_id, f.boarding_at,
               f.fare_paid, f.integration_type,
               tp.latitude AS exact_lat, tp.longitude AS exact_lon
        FROM gold.fares f
        LEFT JOIN gold.trip_positions tp
          ON tp.trip_id = f.trip_id AND tp.origin = 'fare' AND tp.fare_id = f.fare_id
        WHERE f.trip_id = %s
    ),
    bracket AS (
        SELECT fb.fare_id,
               before.latitude AS before_lat, before.longitude AS before_lon,
               before.ping_at AS before_at,
               after.latitude AS after_lat, after.longitude AS after_lon,
               after.ping_at AS after_at
        FROM fare_base fb
        LEFT JOIN LATERAL (
            SELECT latitude, longitude, ping_at
            FROM gold.trip_positions
            WHERE trip_id = fb.trip_id AND origin = 'avl' AND ping_at <= fb.boarding_at
            ORDER BY ping_at DESC LIMIT 1
        ) before ON fb.exact_lat IS NULL
        LEFT JOIN LATERAL (
            SELECT latitude, longitude, ping_at
            FROM gold.trip_positions
            WHERE trip_id = fb.trip_id AND origin = 'avl' AND ping_at >= fb.boarding_at
            ORDER BY ping_at ASC LIMIT 1
        ) after ON fb.exact_lat IS NULL
    )
    SELECT
        fb.fare_id, fb.card_id, fb.passenger_type_id, fb.boarding_at,
        fb.fare_paid, fb.integration_type,
        COALESCE(
            fb.exact_lat,
            br.before_lat + (br.after_lat - br.before_lat)
                * (EXTRACT(EPOCH FROM (fb.boarding_at - br.before_at))
                   / NULLIF(EXTRACT(EPOCH FROM (br.after_at - br.before_at)), 0))
        ) AS latitude,
        COALESCE(
            fb.exact_lon,
            br.before_lon + (br.after_lon - br.before_lon)
                * (EXTRACT(EPOCH FROM (fb.boarding_at - br.before_at))
                   / NULLIF(EXTRACT(EPOCH FROM (br.after_at - br.before_at)), 0))
        ) AS longitude,
        (fb.exact_lat IS NOT NULL) AS location_is_exact
    FROM fare_base fb
    LEFT JOIN bracket br USING (fare_id)
    ORDER BY fb.boarding_at;
"""


def get_connection() -> psycopg.Connection:
    """Open a new autocommit, read-only connection to the database.

    Returns:
        An open connection, in autocommit mode so a single failed query
        can't leave the shared Streamlit session connection stuck
        mid-transaction.

    """
    conn = psycopg.connect(settings.db_dsn)
    conn.autocommit = True
    return conn


def _fetch_frame(
    conn: psycopg.Connection, query: str, params: Sequence[Any]
) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        columns = [d.name for d in cur.description or []]
    return pd.DataFrame.from_records(rows, columns=columns)


def fetch_trip_meta(conn: psycopg.Connection, trip_id: int) -> dict[str, Any] | None:
    """Fetch one trip's bus/route/direction/timing summary.

    Args:
        conn: An open connection.
        trip_id: The `gold.trips.trip_id` to look up.

    Returns:
        A dict of the trip's summary columns, or `None` if `trip_id`
        doesn't exist.

    """
    with conn.cursor() as cur:
        cur.execute(_META_SQL, (trip_id,))
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d.name for d in cur.description or []]
    return dict(zip(columns, row, strict=True))


def fetch_stops(conn: psycopg.Connection, trip_id: int) -> pd.DataFrame:
    """Fetch every stop along the trip's route, with its estimated arrival if any."""
    return _fetch_frame(conn, _STOPS_SQL, (trip_id,))


def fetch_positions(conn: psycopg.Connection, trip_id: int) -> pd.DataFrame:
    """Fetch the trip's ordered AVL (bus GPS) pings."""
    return _fetch_frame(conn, _POSITIONS_SQL, (trip_id,))


def fetch_shape(conn: psycopg.Connection, trip_id: int) -> pd.DataFrame:
    """Fetch the trip's matched GTFS shape (the route the bus is supposed to take)."""
    return _fetch_frame(conn, _SHAPE_SQL, (trip_id,))


def fetch_fares(conn: psycopg.Connection, trip_id: int) -> pd.DataFrame:
    """Fetch the trip's fares, with exact or interpolated boarding locations."""
    return _fetch_frame(conn, _FARES_SQL, (trip_id,))
