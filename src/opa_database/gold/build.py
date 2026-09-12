"""Builds the gold-layer star schema for one month from silver plus two ML tables.

Gold is deliberately not a general reloadable pipeline yet (unlike bronze
and silver's ``replace_period``): it's a one-shot build scoped to whatever
month is requested, dropping and recreating the whole ``gold`` schema each
run. Every table is derived only from ``silver.*`` plus
``ml.trip_validity_final`` (which valid trips exist, and their matched
GTFS route/shape) and ``ml.bus_matching_final_pairs`` (which AVL device a
bus was actually carrying) -- no other ML table is a gold input, and
nothing here re-derives trip validity or bus/device identity itself.
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import TYPE_CHECKING, LiteralString

import psycopg

from opa_database.config import settings

if TYPE_CHECKING:
    from collections.abc import Callable

_logger = logging.getLogger(__name__)

_DECEMBER = 12

# Route 614's AFC direction (0/1) <-> GTFS Ida/Volta (-I/-V shape suffix)
# mapping is reversed relative to every other route in this dataset.
# Verified against GPS ground truth (bus-matching model work, 2026-09-11):
# for 1,415 independently-confirmed bus/device pairs, the GTFS direction a
# device's actual GPS path best correlates with agreed with AFC's stated
# direction (under direction=0 -> I, direction=1 -> V) at >=0.9 for 99.3%
# of bus-device-days -- except route 614, which was consistently the
# opposite (mean agreement 0.03 across 10 buses, 32 days). That fix was
# written, verified, then reverted from the codebase at Victor's request
# before ever being pushed, so this fact lives nowhere else -- it must
# stay hardcoded here.
_REVERSED_DIRECTION_ROUTES = ("614",)

# The company code -> name mapping has no queryable source anywhere in the
# repo: it only exists as inline comments next to the GARAGES coordinate
# list in ml/trip_validity_model/notebooks/05_final_dataset.ipynb. Hand
# transcribed here (11 distinct codes; company_modality confirmed stable
# per code against silver.afc_boardings for November 2023). Two-digit
# convention -- a bus's own first two digits, e.g. "02" -- not silver
# afc_boardings.company_code's separate 3-digit convention ("002").
_BUS_COMPANIES = (
    ("02", 1, "Auto Viação Fortaleza"),
    ("12", 1, "Auto Viação São José"),
    ("14", 1, "Siará Grande"),
    ("20", 1, "Santa Maria"),
    ("21", 1, "Transportes Urbanos Aliança"),
    ("26", 1, "Maraponga Transportes"),
    ("30", 1, "Viação Urbana"),
    ("35", 1, "Vega"),
    ("36", 1, "Santa Cecília"),
    ("42", 1, "Auto Viação Dragão do Mar"),
    ("67", 2, "COOTRAPS"),
)

# A stop-sequence variant is trusted as the shape's real stop list only if
# its stop order correlates this strongly with the stops' own position
# along the shape's geometry (via ST_LineLocatePoint). Chosen from real
# data: of 34 ambiguous (feed, shape) groups in November 2023, every
# legitimate alternate stopping pattern (e.g. short-turn vs full route)
# scored >=0.95, while data-association errors (stop_times rows that
# don't belong with their shape_id) scored far below this, in one checked
# case with stops sitting a median 1.1km from the shape's actual corridor.
_COHERENT_STOP_PATTERN_THRESHOLD = 0.9

_DDL = """
CREATE SCHEMA gold;

CREATE TABLE gold.bus_companies (
    company_code text PRIMARY KEY,
    company_modality integer NOT NULL,
    company_name text NOT NULL
);

CREATE TABLE gold.buses (
    bus_id text PRIMARY KEY,
    company_code text NOT NULL REFERENCES gold.bus_companies (company_code)
);

CREATE TABLE gold.bus_device_intervals (
    bus_id text NOT NULL REFERENCES gold.buses (bus_id),
    device_id text,
    start_date date NOT NULL,
    end_date date NOT NULL,
    confidence double precision,
    method text NOT NULL,
    PRIMARY KEY (bus_id, start_date)
);

CREATE TABLE gold.routes (
    route_id text PRIMARY KEY,
    route_name text
);

CREATE TABLE gold.route_directions (
    route_direction_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    route_id text NOT NULL REFERENCES gold.routes (route_id),
    direction text NOT NULL CHECK (direction IN ('I', 'V')),
    UNIQUE (route_id, direction)
);

CREATE TABLE gold.route_direction_feed (
    route_direction_feed_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    route_direction_id bigint NOT NULL
        REFERENCES gold.route_directions (route_direction_id),
    feed_version_date date NOT NULL,
    shape_id text NOT NULL,
    has_alternate_stop_pattern boolean NOT NULL DEFAULT false,
    UNIQUE (route_direction_id, feed_version_date, shape_id)
);

CREATE TABLE gold.route_direction_shapes (
    route_direction_feed_id bigint NOT NULL
        REFERENCES gold.route_direction_feed (route_direction_feed_id),
    shape_sequence integer NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL,
    geom geometry(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326))
        STORED,
    PRIMARY KEY (route_direction_feed_id, shape_sequence)
);

CREATE TABLE gold.stops (
    stop_id text PRIMARY KEY,
    stop_name text NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL,
    geom geometry(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)) STORED
);

CREATE TABLE gold.route_direction_stops (
    route_direction_feed_id bigint NOT NULL
        REFERENCES gold.route_direction_feed (route_direction_feed_id),
    stop_sequence integer NOT NULL,
    stop_id text NOT NULL REFERENCES gold.stops (stop_id),
    PRIMARY KEY (route_direction_feed_id, stop_sequence)
);

CREATE TABLE gold.journeys (
    journey_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    service_date date NOT NULL,
    bus_id text NOT NULL REFERENCES gold.buses (bus_id),
    line_number text NOT NULL,
    line_shift integer NOT NULL,
    line_opened_at timestamptz NOT NULL,
    line_closed_at timestamptz NOT NULL,
    UNIQUE (
        service_date, bus_id, line_number, line_shift,
        line_opened_at, line_closed_at
    )
);

CREATE TABLE gold.trips (
    trip_id bigint PRIMARY KEY,
    bus_id text NOT NULL REFERENCES gold.buses (bus_id),
    journey_id bigint NOT NULL REFERENCES gold.journeys (journey_id),
    route_direction_feed_id bigint
        REFERENCES gold.route_direction_feed (route_direction_feed_id),
    trip_date date NOT NULL,
    trip_start_timestamp timestamptz NOT NULL,
    trip_end_timestamp timestamptz NOT NULL
);

CREATE TABLE gold.cards (
    card_id text PRIMARY KEY
);

CREATE TABLE gold.passenger_types (
    passenger_type_id integer PRIMARY KEY
);

CREATE TABLE gold.fares (
    -- Not GENERATED ALWAYS AS IDENTITY: this value is set explicitly on
    -- insert (see _load_fares) to the same surrogate row number assigned
    -- to its source row in gold._staging_trip_boardings, so
    -- trip_positions' fare-origin rows can reference the right fare_id
    -- without needing lat/lon to ever exist on this table -- fares is
    -- fare/transaction info only, location lives solely in
    -- trip_positions.
    fare_id bigint PRIMARY KEY,
    trip_id bigint NOT NULL REFERENCES gold.trips (trip_id),
    card_id text NOT NULL REFERENCES gold.cards (card_id),
    passenger_type_id integer NOT NULL
        REFERENCES gold.passenger_types (passenger_type_id),
    event_id text NOT NULL,
    boarding_at timestamptz NOT NULL,
    fare_paid double precision NOT NULL,
    integration_type integer NOT NULL,
    integration_bum integer NOT NULL,
    subsidy_value double precision NOT NULL,
    metro_transfer_value double precision NOT NULL
);

CREATE TABLE gold.trip_positions (
    position_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trip_id bigint NOT NULL REFERENCES gold.trips (trip_id),
    origin text NOT NULL CHECK (origin IN ('avl', 'fare')),
    fare_id bigint REFERENCES gold.fares (fare_id),
    ping_at timestamptz NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL,
    geom geometry(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326))
        STORED,
    speed integer,
    direction integer,
    CHECK ((origin = 'fare') = (fare_id IS NOT NULL))
);
"""

# Built after every table is loaded, matching silver's bulk-then-index
# pattern (loaders/silver.py::replace_period): incremental index
# maintenance during a multi-million-row insert is far slower than one
# batch build afterward, especially for GiST.
#
# Split in two: _INDEXES_BASE runs at the end of phase 1 (build_base),
# _INDEXES_TRIP_POSITIONS at the end of phase 2 (build_trip_positions).
# Building gold.trips(bus_id) as part of the *base* phase (not deferred
# to the end of everything) is deliberate: phase 2's trip_positions join
# reads gold.trips by bus_id, so it should already have this index (and
# real statistics from the phase 1 ANALYZE step) by the time that join
# runs, not just at the very end when it's too late to help.
_INDEXES_BASE: tuple[LiteralString, ...] = (
    "CREATE INDEX ON gold.buses (company_code)",
    "CREATE INDEX ON gold.bus_device_intervals (device_id)",
    "CREATE INDEX ON gold.route_direction_feed (route_direction_id)",
    "CREATE INDEX ON gold.route_direction_feed (feed_version_date)",
    "CREATE INDEX ON gold.route_direction_shapes USING GIST (geom)",
    "CREATE INDEX ON gold.stops USING GIST (geom)",
    "CREATE INDEX ON gold.route_direction_stops (stop_id)",
    "CREATE INDEX ON gold.journeys (bus_id)",
    "CREATE INDEX ON gold.journeys (service_date)",
    "CREATE INDEX ON gold.trips (bus_id)",
    "CREATE INDEX ON gold.trips (route_direction_feed_id)",
    "CREATE INDEX ON gold.trips (trip_date)",
    "CREATE INDEX ON gold.fares (trip_id)",
    "CREATE INDEX ON gold.fares (card_id)",
)
_INDEXES_TRIP_POSITIONS: tuple[LiteralString, ...] = (
    "CREATE INDEX ON gold.trip_positions (trip_id, ping_at)",
    "CREATE INDEX ON gold.trip_positions (fare_id) WHERE fare_id IS NOT NULL",
    "CREATE INDEX ON gold.trip_positions USING GIST (geom)",
)


def get_connection() -> psycopg.Connection:
    """Open a connection to the gold database.

    Returns:
        psycopg.Connection: An open connection to the gold database.

    """
    return psycopg.connect(settings.db_dsn)


def _period_bounds(year: int, month: int) -> tuple[datetime.date, datetime.date]:
    start = datetime.date(year, month, 1)
    end = (
        datetime.date(year + 1, 1, 1)
        if month == _DECEMBER
        else datetime.date(year, month + 1, 1)
    )
    return start, end


def _feed_dates(
    conn: psycopg.Connection, start: datetime.date, end: datetime.date
) -> list[datetime.date]:
    """Every GTFS feed a valid trip in [start, end) actually matched to.

    Deliberately not the calendar-month's own feed exports: a feed stays
    active until superseded, so a trip's ``gtfs_feed_version_date`` can
    predate its own ``trip_date`` by weeks (e.g. a 2023-09-15 export still
    covering early-November trips). Scoping every GTFS dimension query to
    this exact set (rather than a service_date-shaped range) is both
    correct and cheap -- it's usually a handful of dates.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT gtfs_feed_version_date
        FROM ml.trip_validity_final
        WHERE is_valid
          AND trip_date >= %(start)s AND trip_date < %(end)s
          AND gtfs_feed_version_date IS NOT NULL
        """,
        {"start": start, "end": end},
    ).fetchall()
    return [r[0] for r in rows]


def _load_bus_companies(conn: psycopg.Connection) -> None:
    with conn.cursor().copy(
        "COPY gold.bus_companies (company_code, company_modality, company_name) "
        "FROM STDIN"
    ) as copy:
        for row in _BUS_COMPANIES:
            copy.write_row(row)


def _load_buses(
    conn: psycopg.Connection, start: datetime.date, end: datetime.date
) -> None:
    # company_code is always the bus_id's own first two digits -- verified
    # exact for every company appearing in November 2023 (11/11). bus_id
    # is defensively re-padded to 5 digits even though
    # ml.trip_validity_final.bus_id is already documented as always 5.
    conn.execute(
        """
        INSERT INTO gold.buses (bus_id, company_code)
        SELECT DISTINCT lpad(bus_id, 5, '0'), left(lpad(bus_id, 5, '0'), 2)
        FROM ml.trip_validity_final
        WHERE is_valid AND trip_date >= %(start)s AND trip_date < %(end)s
        """,
        {"start": start, "end": end},
    )


def _load_bus_device_intervals(conn: psycopg.Connection) -> None:
    conn.execute("""
        INSERT INTO gold.bus_device_intervals
            (bus_id, device_id, start_date, end_date, confidence, method)
        SELECT p.bus_id, p.device_id, p.start_date, p.end_date, p.confidence, p.method
        FROM ml.bus_matching_final_pairs p
        JOIN gold.buses b ON b.bus_id = p.bus_id
    """)


def _load_routes(conn: psycopg.Connection, feed_dates: list[datetime.date]) -> None:
    # route_id is re-padded from silver's 4-digit GTFS convention
    # ("0042") to gold's 3-digit-minimum convention ("042", but "1074"
    # stays "1074" -- lpad only pads, never truncates), matching
    # ml.trip_validity_final's own route_id format so a trip's route_id
    # needs no further translation anywhere downstream.
    # Latest-feed-wins: route_long_name was verified stable for every
    # route across November's own 2 feeds, but the full feed set a
    # November trip can reference goes back further (see _feed_dates) and
    # wasn't individually re-checked -- this rule is a safe default either
    # way, not just a fallback for a known drift case.
    conn.execute(
        """
        INSERT INTO gold.routes (route_id, route_name)
        SELECT DISTINCT ON (route_id) route_id, route_long_name
        FROM (
            SELECT lpad((route_id::integer)::text, 3, '0') AS route_id,
                   route_long_name, feed_version_date
            FROM silver.gtfs_routes
            WHERE feed_version_date = ANY(%(feed_dates)s)
        ) r
        ORDER BY route_id, feed_version_date DESC
        """,
        {"feed_dates": feed_dates},
    )


def _load_route_directions(
    conn: psycopg.Connection, feed_dates: list[datetime.date]
) -> None:
    # direction_id is 100% NULL in this feed; direction instead lives as a
    # literal -I (Ida) / -V (Volta) suffix on shape_id. route_id re-padded
    # to gold's 3-digit-minimum convention, same as _load_routes.
    conn.execute(
        """
        INSERT INTO gold.route_directions (route_id, direction)
        SELECT DISTINCT lpad((route_id::integer)::text, 3, '0'),
               CASE WHEN shape_id LIKE '%%-I' THEN 'I' ELSE 'V' END
        FROM silver.gtfs_trips
        WHERE feed_version_date = ANY(%(feed_dates)s) AND shape_id IS NOT NULL
        """,
        {"feed_dates": feed_dates},
    )


def _load_route_direction_feed(
    conn: psycopg.Connection, feed_dates: list[datetime.date]
) -> None:
    conn.execute(
        """
        INSERT INTO gold.route_direction_feed
            (route_direction_id, feed_version_date, shape_id)
        SELECT DISTINCT rd.route_direction_id, t.feed_version_date, t.shape_id
        FROM silver.gtfs_trips t
        JOIN gold.route_directions rd
          ON rd.route_id = lpad((t.route_id::integer)::text, 3, '0')
         AND rd.direction = CASE WHEN t.shape_id LIKE '%%-I' THEN 'I' ELSE 'V' END
        WHERE t.feed_version_date = ANY(%(feed_dates)s) AND t.shape_id IS NOT NULL
        """,
        {"feed_dates": feed_dates},
    )


def _load_route_direction_shapes(
    conn: psycopg.Connection, feed_dates: list[datetime.date]
) -> None:
    conn.execute(
        """
        INSERT INTO gold.route_direction_shapes
            (route_direction_feed_id, shape_sequence, latitude, longitude)
        SELECT rdf.route_direction_feed_id, s.shape_pt_sequence,
               s.shape_pt_lat, s.shape_pt_lon
        FROM silver.gtfs_shapes s
        JOIN gold.route_direction_feed rdf
          ON rdf.feed_version_date = s.feed_version_date
         AND rdf.shape_id = s.shape_id
        WHERE s.feed_version_date = ANY(%(feed_dates)s)
        """,
        {"feed_dates": feed_dates},
    )


def _load_stops(conn: psycopg.Connection, feed_dates: list[datetime.date]) -> None:
    # Latest-feed-wins: 125 of 5,276 stops (~2.4%) genuinely drift in
    # name/lat/lon across the feeds relevant to November 2023 -- a real
    # agency correction over time, not noise, so the most recent version
    # is the right one to keep.
    conn.execute(
        """
        INSERT INTO gold.stops (stop_id, stop_name, latitude, longitude)
        SELECT DISTINCT ON (stop_id) stop_id, stop_name, stop_lat, stop_lon
        FROM silver.gtfs_stops
        WHERE feed_version_date = ANY(%(feed_dates)s)
        ORDER BY stop_id, feed_version_date DESC
        """,
        {"feed_dates": feed_dates},
    )


def _load_route_direction_stops(
    conn: psycopg.Connection, feed_dates: list[datetime.date]
) -> None:
    """Canonicalize each route-direction-feed's stop list.

    A shape's stop list isn't structurally guaranteed 1:1 in GTFS (it
    lives on individual scheduled trips via `stop_times`, not the shape
    itself), and in this data it genuinely isn't for 34 of 1,234 (feed,
    shape) groups. Checked against real data before picking a rule:
    majority-trip-count is unsafe on its own (one shape had its single
    largest-count variant be a data error, correlating at 0.164 with the
    shape's own geometry while three smaller variants -- collectively more
    trips -- all scored 0.998). So: score every variant by how well its
    stop order tracks the stop's own position along the shape (via
    `ST_LineLocatePoint`), discard anything below
    `_COHERENT_STOP_PATTERN_THRESHOLD` as a data error rather than a real
    alternate pattern, then take the most-used variant among what's left.
    `has_alternate_stop_pattern` flags feeds where more than one coherent
    variant existed (a real short-turn/full-route split), so the
    approximation is traceable rather than silent.
    """
    conn.execute(
        """
        CREATE TEMP TABLE stop_pattern_choice ON COMMIT DROP AS
        WITH shape_lines AS (
            SELECT feed_version_date, shape_id,
                   ST_MakeLine(geom ORDER BY shape_pt_sequence) AS line
            FROM silver.gtfs_shapes
            WHERE feed_version_date = ANY(%(feed_dates)s)
            GROUP BY feed_version_date, shape_id
        ),
        trip_stop_seqs AS (
            SELECT t.feed_version_date, t.shape_id, t.trip_id,
                   array_agg(st.stop_id ORDER BY st.stop_sequence) AS stop_seq
            FROM silver.gtfs_trips t
            JOIN silver.gtfs_stop_times st
              ON st.feed_version_date = t.feed_version_date
             AND st.trip_id = t.trip_id
            WHERE t.feed_version_date = ANY(%(feed_dates)s)
              AND t.shape_id IS NOT NULL
            GROUP BY t.feed_version_date, t.shape_id, t.trip_id
        ),
        variants AS (
            SELECT feed_version_date, shape_id, stop_seq, COUNT(*) AS n_trips
            FROM trip_stop_seqs
            GROUP BY feed_version_date, shape_id, stop_seq
        ),
        variant_fit AS (
            SELECT v.feed_version_date, v.shape_id, v.stop_seq, v.n_trips,
                   corr(u.ord, ST_LineLocatePoint(sl.line, s.geom)) AS geometry_fit
            FROM variants v
            JOIN shape_lines sl USING (feed_version_date, shape_id)
            CROSS JOIN LATERAL unnest(v.stop_seq) WITH ORDINALITY AS u(stop_id, ord)
            JOIN silver.gtfs_stops s
              ON s.feed_version_date = v.feed_version_date AND s.stop_id = u.stop_id
            GROUP BY v.feed_version_date, v.shape_id, v.stop_seq, v.n_trips
        ),
        ranked AS (
            SELECT *,
                ROW_NUMBER() OVER (
                    PARTITION BY feed_version_date, shape_id
                    ORDER BY (geometry_fit >= %(threshold)s) DESC,
                             n_trips DESC, geometry_fit DESC
                ) AS rn,
                COUNT(*) FILTER (WHERE geometry_fit >= %(threshold)s)
                    OVER (PARTITION BY feed_version_date, shape_id) AS n_coherent
            FROM variant_fit
        )
        SELECT feed_version_date, shape_id, stop_seq, n_coherent
        FROM ranked
        WHERE rn = 1
        """,
        {"feed_dates": feed_dates, "threshold": _COHERENT_STOP_PATTERN_THRESHOLD},
    )
    conn.execute("""
        UPDATE gold.route_direction_feed rdf
        SET has_alternate_stop_pattern = true
        FROM stop_pattern_choice c
        WHERE c.feed_version_date = rdf.feed_version_date
          AND c.shape_id = rdf.shape_id
          AND c.n_coherent > 1
    """)
    conn.execute("""
        INSERT INTO gold.route_direction_stops
            (route_direction_feed_id, stop_sequence, stop_id)
        SELECT rdf.route_direction_feed_id, u.ord, u.stop_id
        FROM stop_pattern_choice c
        JOIN gold.route_direction_feed rdf
          ON rdf.feed_version_date = c.feed_version_date
         AND rdf.shape_id = c.shape_id
        CROSS JOIN LATERAL unnest(c.stop_seq) WITH ORDINALITY AS u(stop_id, ord)
    """)


def _load_trip_source_attrs(
    conn: psycopg.Connection, start: datetime.date, end: datetime.date
) -> None:
    """Materialize every valid trip's attributes rejoined from raw silver.

    `ml.trip_validity_final` groups trips out of `silver.afc_boardings`
    without a formula back to the source rows, but its natural key does
    the job: `(trip_date, zero-padded bus_id, trip_start_timestamp,
    trip_end_timestamp)` is a verified clean 1:1 grouping, so this is the
    single join every other trip-shaped gold table (journeys, trips,
    fares) is built from -- computed once here rather than per table.
    """
    conn.execute(
        """
        CREATE TEMP TABLE trip_source_attrs ON COMMIT DROP AS
        SELECT
            f.trip_id, f.bus_id, f.trip_date,
            f.trip_start_timestamp, f.trip_end_timestamp,
            f.route_id, f.gtfs_feed_version_date,
            f.gtfs_shape_id_i, f.gtfs_shape_id_v,
            MIN(b.direction) AS afc_direction,
            MIN(b.line_number) AS line_number,
            MIN(b.line_shift) AS line_shift,
            MIN(b.line_opened_at) AS line_opened_at,
            MIN(b.line_closed_at) AS line_closed_at
        FROM ml.trip_validity_final f
        JOIN silver.afc_boardings b
          ON b.service_date = f.trip_date
         AND (CASE WHEN length(b.vehicle_number) < 5
                   THEN lpad(b.vehicle_number, 5, '0')
                   ELSE b.vehicle_number END) = f.bus_id
         AND b.trip_opened_at = f.trip_start_timestamp
         AND b.trip_closed_at = f.trip_end_timestamp
        WHERE f.is_valid AND f.trip_date >= %(start)s AND f.trip_date < %(end)s
        GROUP BY f.trip_id, f.bus_id, f.trip_date, f.trip_start_timestamp,
                 f.trip_end_timestamp, f.route_id, f.gtfs_feed_version_date,
                 f.gtfs_shape_id_i, f.gtfs_shape_id_v
        """,
        {"start": start, "end": end},
    )
    conn.execute(
        "CREATE INDEX ON trip_source_attrs (trip_date, bus_id, "
        "trip_start_timestamp, trip_end_timestamp)"
    )


def _load_journeys(conn: psycopg.Connection) -> None:
    conn.execute("""
        INSERT INTO gold.journeys
            (service_date, bus_id, line_number, line_shift,
             line_opened_at, line_closed_at)
        SELECT DISTINCT
            trip_date, bus_id, line_number, line_shift,
            line_opened_at, line_closed_at
        FROM trip_source_attrs
    """)


def _load_trips(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        INSERT INTO gold.trips
            (trip_id, bus_id, journey_id, route_direction_feed_id,
             trip_date, trip_start_timestamp, trip_end_timestamp)
        SELECT
            a.trip_id, a.bus_id, j.journey_id, rdf.route_direction_feed_id,
            a.trip_date, a.trip_start_timestamp, a.trip_end_timestamp
        FROM trip_source_attrs a
        JOIN gold.journeys j
          ON j.service_date = a.trip_date AND j.bus_id = a.bus_id
         AND j.line_number = a.line_number AND j.line_shift = a.line_shift
         AND j.line_opened_at = a.line_opened_at
         AND j.line_closed_at = a.line_closed_at
        LEFT JOIN gold.route_direction_feed rdf
          ON rdf.feed_version_date = a.gtfs_feed_version_date
         AND rdf.shape_id = (
             CASE
                 WHEN a.route_id = ANY(%(reversed_routes)s) THEN
                     CASE a.afc_direction WHEN 0 THEN a.gtfs_shape_id_v
                                           ELSE a.gtfs_shape_id_i END
                 ELSE
                     CASE a.afc_direction WHEN 0 THEN a.gtfs_shape_id_i
                                           ELSE a.gtfs_shape_id_v END
             END
         )
        """,
        {"reversed_routes": list(_REVERSED_DIRECTION_ROUTES)},
    )


def _load_trip_boardings(
    conn: psycopg.Connection, start: datetime.date, end: datetime.date
) -> None:
    """Materialize every matched boarding row once, for cards/types/fares to share.

    `gold.cards`, `gold.passenger_types`, `gold.fares`, and (in a later,
    separately-committed phase) `gold.trip_positions`' fare-origin rows
    all need the same trip_source_attrs-to-silver.afc_boardings join;
    doing it three separate times over ~15M November rows (rather than
    once here) was the single biggest cost in an earlier build attempt.

    A real table (`gold._staging_trip_boardings`), not a session-local
    TEMP one: it needs to survive from this phase (build_base) into the
    next (build_trip_positions), which commits and reconnects separately.
    Not part of the public gold schema -- dropped once by `build()` after
    both phases finish -- but plain `gold.*` rather than a second schema
    since `DROP SCHEMA gold CASCADE` at the start of every build_base run
    already cleans it up automatically even if a previous run's cleanup
    was skipped.

    The literal `b.service_date` bound is not redundant with joining to
    trip_source_attrs (already scoped to this exact period): without a
    literal date range in the query text, the planner has no way to know
    afc_boardings' rows can be filtered before the join, since a temp
    table's actual contents aren't visible to it as a bound the way a
    WHERE literal is. Confirmed the hard way -- omitting this caused a
    full scan of afc_boardings' entire ~395M-row history (every month
    ever loaded, not just this one) instead of an index scan restricted
    to November, spilling 43GB+ of temp files before being killed.
    """
    conn.execute(
        """
        CREATE TABLE gold._staging_trip_boardings AS
        SELECT
            row_number() OVER () AS fare_id,
            a.trip_id, b.card_id, b.passenger_type, b.event_id, b.boarding_at,
            b.fare_paid, b.integration_type, b.integration_bum, b.subsidy_value,
            b.metro_transfer_value, b.latitude, b.longitude
        FROM silver.afc_boardings b
        JOIN trip_source_attrs a
          ON a.trip_date = b.service_date
         AND a.bus_id = (CASE WHEN length(b.vehicle_number) < 5
                               THEN lpad(b.vehicle_number, 5, '0')
                               ELSE b.vehicle_number END)
         AND a.trip_start_timestamp = b.trip_opened_at
         AND a.trip_end_timestamp = b.trip_closed_at
        WHERE b.service_date >= %(start)s AND b.service_date < %(end)s
        """,
        {"start": start, "end": end},
    )


def _load_cards_and_passenger_types(conn: psycopg.Connection) -> None:
    conn.execute("""
        INSERT INTO gold.cards (card_id)
        SELECT DISTINCT card_id FROM gold._staging_trip_boardings
    """)
    conn.execute("""
        INSERT INTO gold.passenger_types (passenger_type_id)
        SELECT DISTINCT passenger_type FROM gold._staging_trip_boardings
    """)


def _load_fares(conn: psycopg.Connection) -> None:
    # fare_id is trip_boardings' own row number, not a fresh identity value
    # -- see gold.fares' DDL comment for why (lets trip_positions attach
    # fare-origin locations without fares itself ever carrying lat/lon).
    conn.execute("""
        INSERT INTO gold.fares
            (fare_id, trip_id, card_id, passenger_type_id, event_id,
             boarding_at, fare_paid, integration_type, integration_bum,
             subsidy_value, metro_transfer_value)
        SELECT
            fare_id, trip_id, card_id, passenger_type, event_id, boarding_at,
            fare_paid, integration_type, integration_bum, subsidy_value,
            metro_transfer_value
        FROM gold._staging_trip_boardings
    """)


def _load_trip_positions(
    conn: psycopg.Connection, start: datetime.date, end: datetime.date
) -> None:
    # AVL-origin: only for trips whose bus resolves to a device for that
    # trip's date (gold.bus_device_intervals), scoped to avl_pings'
    # own monthly partition for a cheap partition-pruned scan. A trip with
    # no device match simply gets no rows here -- it still exists in
    # gold.trips and gold.fares.
    #
    # The inner SELECT is written as a LATERAL subquery with a trailing
    # OFFSET 0, not a plain JOIN -- confirmed necessary the hard way. A
    # plain JOIN (even textually written in trips-first order) gets
    # flattened by the planner into a free join reordering that joins
    # bus_device_intervals straight to avl_pings on device_id alone,
    # before ever applying a trip's own narrow time window: that plan
    # sorts/hashes the *entire* month's ~131M-row avl_pings partition
    # (43GB+ of spilled temp files, 20+ minutes, still running when
    # killed). OFFSET 0 is a standard Postgres idiom that blocks subquery
    # flattening, forcing genuine per-row correlated execution -- one
    # indexed lookup per (trip, device) pair using
    # avl_pings_*_device_ts_idx, exactly as intended. Verified via
    # EXPLAIN (ANALYZE, TIMING OFF) against real November 2023 data: zero
    # temp spill, 820s end to end for the real output size (~109M rows --
    # 720K trips average ~152 pings each, consistent with normal AVL
    # ping frequency over a typical trip's duration, not a bug).
    conn.execute(
        """
        INSERT INTO gold.trip_positions
            (trip_id, origin, ping_at, latitude, longitude, speed, direction)
        SELECT t.trip_id, 'avl', p.metric_timestamp, p.latitude, p.longitude,
               p.speed, p.direction %% 360
        FROM gold.trips t
        JOIN gold.bus_device_intervals bdi
          ON bdi.bus_id = t.bus_id
         AND bdi.device_id IS NOT NULL
         AND t.trip_date BETWEEN bdi.start_date AND bdi.end_date
        CROSS JOIN LATERAL (
            SELECT metric_timestamp, latitude, longitude, speed, direction
            FROM silver.avl_pings av
            WHERE av.device_id = bdi.device_id
              AND av.metric_timestamp >= t.trip_start_timestamp
              AND av.metric_timestamp <= t.trip_end_timestamp
              AND av.metric_timestamp >= %(start)s AND av.metric_timestamp < %(end)s
            OFFSET 0
        ) p
        """,
        {"start": start, "end": end},
    )
    # Fare-origin: every fare that happens to carry a GPS coordinate. Reads
    # location from gold._staging_trip_boardings, not gold.fares -- fares
    # carries no lat/lon at all, only trip_positions does. fare_id here is
    # exactly _staging_trip_boardings.fare_id, the same value _load_fares
    # used as the corresponding gold.fares row's PK, so this is a valid FK
    # with no join needed.
    conn.execute("""
        INSERT INTO gold.trip_positions
            (trip_id, origin, fare_id, ping_at, latitude, longitude)
        SELECT trip_id, 'fare', fare_id, boarding_at, latitude, longitude
        FROM gold._staging_trip_boardings
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """)


def _create_indexes(
    conn: psycopg.Connection, statements: tuple[LiteralString, ...]
) -> None:
    for statement in statements:
        conn.execute(statement)


def _step(label: str, fn: Callable[[], None]) -> None:
    """Run one build step with start/elapsed console output.

    A deliberate exception to this codebase's usual silent-library
    convention: every step here can plausibly take minutes against
    hundreds of millions of source rows with no other feedback otherwise
    (this whole build runs inside one transaction, so nothing is visible
    to another connection until it commits) -- operator visibility into
    which step is running, and how long it took, matters more here than
    it does for a fast per-partition silver load.

    Args:
        label (str): Human-readable step name to print.
        fn (Callable[[], None]): The step to run, taking no arguments
            (wrap with a lambda/partial to bind a step's real arguments).

    """
    start_time = time.monotonic()
    _logger.info("%s...", label)
    fn()
    _logger.info("%s done (%.1fs)", label, time.monotonic() - start_time)


def build_base(year: int, month: int) -> None:
    """Build every gold table except trip_positions, and commit.

    Phase 1 of 2 -- see `build_trip_positions` for phase 2, and `build`
    for running both in sequence. Split into two independently-committed
    phases (rather than one all-or-nothing transaction) specifically so
    `gold.trips`/`gold.bus_device_intervals` can be inspected with a real
    `EXPLAIN` from another session and `build_trip_positions` re-run
    repeatedly while tuning it, without re-paying this phase's cost (a
    few minutes) on every attempt.

    Args:
        year (int): Calendar year to build gold for.
        month (int): Calendar month to build gold for.

    """
    start, end = _period_bounds(year, month)

    def _create_schema() -> None:
        conn.execute("DROP SCHEMA IF EXISTS gold CASCADE")
        conn.execute(_DDL)

    with get_connection() as conn, conn.transaction():
        # Scoped to this transaction only (SET LOCAL, not the session-wide
        # default). Postgres's 4MB default work_mem forces the
        # multi-million-row hash joins below into multi-batch, disk-spilling
        # execution -- verified directly against pg_stat_activity during an
        # earlier build attempt (DataFileRead wait event, 5+ minutes on a
        # single join step that should run in-memory). Parallel workers are
        # disabled rather than left to the planner's discretion: this
        # container's /dev/shm is only 64MB, and a parallel hash join at
        # 512MB work_mem exhausted it outright (DiskFull resizing a shared
        # memory segment) -- a serial hash join still gets the work_mem
        # benefit from ordinary process memory, no shared segment needed.
        # Every phase (this one and build_trip_positions) sets these fresh:
        # SET LOCAL only lasts for the transaction it's issued in.
        conn.execute("SET LOCAL work_mem = '512MB'")
        conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")

        _step("drop/create schema", _create_schema)

        _step("bus_companies", lambda: _load_bus_companies(conn))
        _step("buses", lambda: _load_buses(conn, start, end))
        _step("bus_device_intervals", lambda: _load_bus_device_intervals(conn))

        feed_dates: list[datetime.date] = []

        def _resolve_feed_dates() -> None:
            feed_dates.extend(_feed_dates(conn, start, end))

        _step("resolve feed dates", _resolve_feed_dates)
        _step("routes", lambda: _load_routes(conn, feed_dates))
        _step("route_directions", lambda: _load_route_directions(conn, feed_dates))
        _step(
            "route_direction_feed",
            lambda: _load_route_direction_feed(conn, feed_dates),
        )
        _step(
            "route_direction_shapes",
            lambda: _load_route_direction_shapes(conn, feed_dates),
        )
        _step("stops", lambda: _load_stops(conn, feed_dates))
        _step(
            "route_direction_stops",
            lambda: _load_route_direction_stops(conn, feed_dates),
        )

        _step(
            "trip_source_attrs (rejoin trips to silver.afc_boardings)",
            lambda: _load_trip_source_attrs(conn, start, end),
        )
        _step("journeys", lambda: _load_journeys(conn))
        _step("trips", lambda: _load_trips(conn))
        _step(
            "trip_boardings (rejoin fares to silver.afc_boardings)",
            lambda: _load_trip_boardings(conn, start, end),
        )
        _step(
            "cards + passenger_types",
            lambda: _load_cards_and_passenger_types(conn),
        )
        _step("fares", lambda: _load_fares(conn))
        _step("base indexes", lambda: _create_indexes(conn, _INDEXES_BASE))

        def _analyze_before_positions() -> None:
            # gold.trips and gold.bus_device_intervals were just bulk-loaded
            # inside this same still-open transaction, so they carry zero
            # statistics -- autovacuum can't touch an uncommitted
            # transaction's tables, and nothing else runs ANALYZE
            # automatically here. Confirmed the hard way: without this, a
            # later trip_positions join against these two freshly-loaded
            # tables took 20+ minutes; the same join shape against
            # permanently-analyzed tables (verified via a standalone
            # EXPLAIN against ml.trip_validity_final/bus_matching_final_pairs)
            # used proper indexes throughout, so the missing statistics --
            # not the join's inherent size -- were the actual cause. Done
            # here, at the end of phase 1, rather than at the start of
            # phase 2: analyzing right after these tables are loaded (and
            # their own indexes just built) means phase 2 can be re-run
            # repeatedly without repeating this too.
            conn.execute("ANALYZE gold.trips")
            conn.execute("ANALYZE gold.bus_device_intervals")

        _step("analyze trips + bus_device_intervals", _analyze_before_positions)
    # Transaction commits here.


def build_trip_positions(year: int, month: int) -> None:
    """Build gold.trip_positions and commit. Phase 2 of 2, independently re-runnable.

    Requires `build_base` to have already committed for this period --
    reads `gold._staging_trip_boardings` (persisted there, not rebuilt
    here) for fare-origin positions' lat/lon. Safe to call more than once
    while tuning this step: clears out any existing `gold.trip_positions`
    rows first.

    Args:
        year (int): Calendar year to build gold for.
        month (int): Calendar month to build gold for.

    """
    start, end = _period_bounds(year, month)
    with get_connection() as conn, conn.transaction():
        conn.execute("SET LOCAL work_mem = '512MB'")
        conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")

        def _clear_trip_positions() -> None:
            conn.execute("TRUNCATE gold.trip_positions")

        _step("clear trip_positions", _clear_trip_positions)
        _step("trip_positions", lambda: _load_trip_positions(conn, start, end))
        _step(
            "trip_positions indexes",
            lambda: _create_indexes(conn, _INDEXES_TRIP_POSITIONS),
        )


def build(year: int, month: int) -> None:
    """Build the entire gold schema from scratch for one calendar month.

    Runs `build_base` then `build_trip_positions` in sequence, each its
    own committed phase, then drops `gold._staging_trip_boardings` --
    internal scratch state, not part of the public gold schema. Not
    dropped inside `build_trip_positions` itself, since that needs to
    stay freely re-runnable (e.g. while tuning it) without requiring
    `build_base` to run again first.

    Args:
        year (int): Calendar year to build gold for.
        month (int): Calendar month to build gold for.

    """
    build_base(year, month)
    build_trip_positions(year, month)
    with get_connection() as conn, conn.transaction():
        conn.execute("DROP TABLE gold._staging_trip_boardings")
