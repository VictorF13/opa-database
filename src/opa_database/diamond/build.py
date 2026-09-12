"""Builds diamond.stop_arrivals: interpolated bus stop arrival times.

Turns gold's raw AVL breadcrumbs into a clean, ordered table of exactly
when each bus crossed each stop on its route, for one month at a time.
Reads only from `gold.*` -- never silver or ml directly, matching the
medallion layering (diamond sits on top of gold, the same way gold sits
on top of silver/ml).

Pipeline, per trip:

1. Drop pings sitting at `(0, 0)` (a GPS "no fix" sentinel, not a real
   position -- see gold's own fare-position finding of the same issue)
   before any other computation touches them.
2. Segment the whole trip's ping sequence into runs, split wherever the
   implied speed between two consecutive pings is physically impossible,
   and keep every run with 2 or more pings -- a trip can genuinely go
   sane, then a glitch burst, then sane again, and keeping only the
   single largest run would silently discard that second real stretch
   along with the glitch. Whole-trip, not a local (single- or 3-point)
   check: a bad ping sitting right at the start or end of a trip's
   sequence has only one neighbour, so no "do its neighbours disagree
   with it" check can catch it directly -- but it still can't form a run
   of 2 or more with anything, so it's dropped all the same.
3. Project every remaining ping and every stop onto the trip's route
   shape as a 0..1 fraction of the route's length
   (`ST_LineLocatePoint`).
4. Track the *running maximum* of that fraction over time. Being
   non-decreasing by construction, it can never register a stop the bus
   already passed, and a stop can only ever be "first reached" once --
   this is what keeps arrivals monotonic in stop order and immune to a
   bus reversing or idling, without needing a stateful per-stop walk.
5. For each stop, the arrival is bracketed by the last ping before the
   running max first reached that stop's fraction, and the ping that
   pushed it there. If either bracketing ping's real position is too far
   from the route's actual shape (a detour), the stop is dropped rather
   than given a fabricated time.
   - A stop whose fraction is at or before the trip's very first ping
     has no earlier ping to bracket it at all -- the same mechanism
     naturally reuses the trip's first two pings as an extrapolation
     line instead (a slightly-before-the-first-ping "departure" time),
     bounded by a real distance check against that first ping so it
     never extrapolates across an implausibly large gap.
   - A stop beyond the trip's last-reached fraction gets the symmetric
     treatment: extrapolated forward from the trip's last two pings,
     under the same distance bound against the last ping.
6. The arrival time is linearly interpolated (or, for the two boundary
   cases above, extrapolated) between the bracket's two timestamps,
   weighted by how far along the stop's fraction sits between the two
   pings' own fractions. Confidence is graded by the gap between those
   two timestamps -- sparse-but-on-route data still gets an answer, just
   a lower-confidence one, rather than being dropped like a genuine
   detour is.

Known, deliberately accepted gap: roughly 3% of gold's route_direction_feed
rows have a stop_sequence that isn't perfectly monotonic in route-fraction
(the upstream canonicalization only required high correlation with the
shape geometry, not perfect monotonicity). Left as-is for now.
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

# Validated against real November 2023 gold.trip_positions data before
# adopting (see the session that built this module): median implied
# speed between consecutive pings is 18 km/h, p99 is 54.2 km/h, and only
# 0.17% of consecutive-ping pairs exceed 100 km/h -- consistent with GPS
# "bounce" anomalies being rare outliers, not an overly aggressive cutoff
# on real fast movement.
_SPEED_LIMIT_KMH = 100

# Also validated against real data: median cross-track distance from an
# AVL ping to its own route's shape is 3.6m, p90 is 12.1m, but p99 jumps
# to 209m -- a real distributional break between "on route" and
# "genuinely elsewhere," with 100m sitting cleanly in the gap.
_CROSS_TRACK_THRESHOLD_M = 100

# How close a stop must be to the trip's first/last ping to be worth
# extrapolating to, rather than left unmatched. Reuses the cross-track
# threshold's value rather than an independently-validated number of its
# own -- both questions are "is this position plausibly still on this
# same stretch of route," so the same order of magnitude applies.
_BOUNDARY_EXTRAPOLATION_MAX_M = _CROSS_TRACK_THRESHOLD_M

# Safety net, not a semantic threshold: bounds how many multiples of the
# reference segment's own length an extrapolation is allowed to reach.
# The real gate on "is this extrapolation reasonable" is the physical
# distance check above -- but a bus idling can produce two boundary pings
# whose *fraction* barely differs even though their real positions are
# fine, and dividing by that near-zero fraction gap can blow the
# extrapolated timestamp arithmetic outside Postgres's valid range
# entirely (`timestamp out of range`, hit on real November 2023 data
# while building this module). This catches that degenerate case
# regardless of what the distance check says.
_MAX_EXTRAPOLATION_FACTOR = 10

# The actual semantic gate on a boundary extrapolation (as opposed to
# _MAX_EXTRAPOLATION_FACTOR, which only exists to keep the arithmetic
# from overflowing): an extrapolated arrival more than a minute away from
# the reference ping it was extrapolated from is not trustworthy enough
# to keep, regardless of how close the stop is physically.
_MAX_EXTRAPOLATION_SECONDS = 60

# Confidence grading, per the original spec: High <=60s, Medium
# 60-180s, Low >180s.
_HIGH_CONFIDENCE_GAP_S = 60
_MEDIUM_CONFIDENCE_GAP_S = 180

_DDL = """
CREATE SCHEMA diamond;

CREATE TABLE diamond.stop_arrivals (
    arrival_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trip_id bigint NOT NULL REFERENCES gold.trips (trip_id),
    route_direction_feed_id bigint NOT NULL,
    stop_sequence integer NOT NULL,
    arrival_time timestamptz NOT NULL,
    ping_gap_seconds integer NOT NULL,
    confidence text NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    UNIQUE (trip_id, stop_sequence),
    FOREIGN KEY (route_direction_feed_id, stop_sequence)
        REFERENCES gold.route_direction_stops (route_direction_feed_id, stop_sequence)
);
"""

_INDEXES: tuple[LiteralString, ...] = (
    "CREATE INDEX ON diamond.stop_arrivals (route_direction_feed_id, stop_sequence)",
    "CREATE INDEX ON diamond.stop_arrivals (arrival_time)",
)


def get_connection() -> psycopg.Connection:
    """Open a connection to the diamond database.

    Returns:
        psycopg.Connection: An open connection to the diamond database.

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


def _step(label: str, fn: Callable[[], None]) -> None:
    """Run one build step with start/elapsed console output.

    See gold/build.py::_step for why this deviates from the codebase's
    usual silent-library convention: an operator watching a multi-minute
    build needs to see which step is running.

    Args:
        label (str): Human-readable step name to print.
        fn (Callable[[], None]): The step to run, taking no arguments.

    """
    start_time = time.monotonic()
    _logger.info("%s...", label)
    fn()
    _logger.info("%s done (%.1fs)", label, time.monotonic() - start_time)


def _load_clean_pings(
    conn: psycopg.Connection, start: datetime.date, end: datetime.date
) -> None:
    """Materialize AVL pings with (0,0) sentinels and insane-speed runs removed.

    Whole-trip run segmentation, not a local (single- or 3-point) check:
    every consecutive pair gets flagged as a "bad edge" if the implied
    speed between them is physically impossible, then a running count of
    bad edges crossed so far (a plain cumulative SUM window function, no
    recursion) assigns each ping a run id -- every bad edge increments
    it, so pings between two bad edges share one id.

    Every run with at least 2 pings survives, not just the single largest
    one: a trip can genuinely go sane -> glitch burst -> sane again, and
    keeping only the biggest run would silently throw away that second
    real stretch of data along with the glitch. A run of exactly 1 ping,
    by contrast, has no internal edge of its own to have ever been
    validated as sane in the first place -- that's the specific,
    narrower case this drops. (A ping_progress/interior_crossings bracket
    spanning the resulting gap between two kept runs just gets a larger
    ping_gap_seconds and a correspondingly lower confidence, the same as
    any other sparse-data case -- no special-casing needed there.)

    This also fixes a real gap a purely local (single- or 3-point) check
    has: a bad ping sitting at the very start or end of a trip's sequence
    has only one neighbour, so no "do both neighbours disagree with it"
    check can ever catch it directly -- but it still can't form a run of
    2 or more with anything, so it's dropped all the same.
    """
    conn.execute(
        """
        CREATE TEMP TABLE clean_pings ON COMMIT DROP AS
        WITH base AS (
            SELECT tp.trip_id, tp.ping_at, tp.geom, tp.latitude, tp.longitude
            FROM gold.trip_positions tp
            JOIN gold.trips t USING (trip_id)
            WHERE tp.origin = 'avl'
              AND NOT (tp.latitude = 0 AND tp.longitude = 0)
              AND t.trip_date >= %(start)s AND t.trip_date < %(end)s
              AND t.route_direction_feed_id IS NOT NULL
        ),
        edges AS (
            SELECT *,
                LAG(ping_at) OVER w AS prev_at, LAG(geom) OVER w AS prev_geom
            FROM base
            WINDOW w AS (PARTITION BY trip_id ORDER BY ping_at)
        ),
        flagged AS (
            SELECT *,
                prev_at IS NOT NULL AND ping_at != prev_at
                    AND (ST_Distance(geom::geography, prev_geom::geography)
                         / EXTRACT(EPOCH FROM (ping_at - prev_at)) * 3.6)
                        > %(limit)s AS bad_edge_before
            FROM edges
        ),
        grouped AS (
            SELECT trip_id, ping_at, geom, latitude, longitude,
                SUM(CASE WHEN bad_edge_before THEN 1 ELSE 0 END)
                    OVER (PARTITION BY trip_id ORDER BY ping_at) AS run_id
            FROM flagged
        ),
        run_sizes AS (
            SELECT trip_id, run_id, COUNT(*) AS n
            FROM grouped
            GROUP BY trip_id, run_id
        )
        SELECT g.trip_id, g.ping_at, g.geom, g.latitude, g.longitude
        FROM grouped g
        JOIN run_sizes rs USING (trip_id, run_id)
        WHERE rs.n >= 2
        """,
        {"start": start, "end": end, "limit": _SPEED_LIMIT_KMH},
    )
    conn.execute("CREATE INDEX ON clean_pings (trip_id, ping_at)")
    conn.execute("ANALYZE clean_pings")


def _load_route_lines(conn: psycopg.Connection) -> None:
    # Not scoped by feed_dates: gold.route_direction_feed is already a
    # small table (thousands of rows for a whole month), built once by
    # the gold layer -- no need to re-filter it here.
    conn.execute(
        """
        CREATE TEMP TABLE route_lines ON COMMIT DROP AS
        SELECT rdf.route_direction_feed_id,
               ST_MakeLine(s.geom ORDER BY s.shape_sequence) AS line
        FROM gold.route_direction_shapes s
        JOIN gold.route_direction_feed rdf USING (route_direction_feed_id)
        GROUP BY rdf.route_direction_feed_id
        """
    )
    conn.execute("CREATE UNIQUE INDEX ON route_lines (route_direction_feed_id)")
    conn.execute("ANALYZE route_lines")


def _load_stop_fractions(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TEMP TABLE stop_fractions ON COMMIT DROP AS
        SELECT rds.route_direction_feed_id, rds.stop_sequence,
               ST_LineLocatePoint(rl.line, s.geom) AS fraction, s.geom AS stop_geom
        FROM gold.route_direction_stops rds
        JOIN gold.stops s USING (stop_id)
        JOIN route_lines rl USING (route_direction_feed_id)
        """
    )
    conn.execute("CREATE INDEX ON stop_fractions (route_direction_feed_id, fraction)")
    conn.execute("ANALYZE stop_fractions")


def _load_ping_progress(conn: psycopg.Connection) -> None:
    """Project cleaned pings onto their route and track running-max progress.

    `running_max_fraction` is the key trick that replaces a stateful
    per-stop walk: being non-decreasing by construction, the first ping
    where it reaches a given stop's fraction is that stop's one and only
    genuine "first crossing," immune to the bus reversing or idling
    afterward. See this module's docstring for the full reasoning.
    """
    conn.execute(
        """
        CREATE TEMP TABLE ping_progress ON COMMIT DROP AS
        SELECT
            cp.trip_id, t.route_direction_feed_id,
            cp.ping_at, cp.geom,
            ST_LineLocatePoint(rl.line, cp.geom) AS fraction,
            MAX(ST_LineLocatePoint(rl.line, cp.geom)) OVER w AS running_max_fraction,
            LAG(cp.ping_at) OVER w AS prev_ping_at,
            LAG(cp.geom) OVER w AS prev_geom,
            LAG(ST_LineLocatePoint(rl.line, cp.geom)) OVER w AS prev_fraction
        FROM clean_pings cp
        JOIN gold.trips t USING (trip_id)
        JOIN route_lines rl USING (route_direction_feed_id)
        WINDOW w AS (PARTITION BY cp.trip_id ORDER BY cp.ping_at)
        """
    )
    conn.execute("CREATE INDEX ON ping_progress (trip_id, running_max_fraction)")
    conn.execute("ANALYZE ping_progress")


def _load_interior_crossings(conn: psycopg.Connection) -> None:
    """Pick each stop's one genuine crossing, interior to the trip's pings.

    `DISTINCT ON` picks the first (by ping_at) row where the running max
    reaches a stop's fraction -- the plain, non-recursive equivalent of
    "first time the bus's progress reaches this stop." For a normal
    interior stop this row's preceding ping has a fraction strictly below
    the stop's; for a stop at or before the trip's very first ping (no
    earlier ping exists at all), this naturally resolves to the trip's
    first two pings instead, with the stop's fraction possibly *below*
    the first ping's own fraction -- an extrapolation backward in time,
    handled by `_load_stop_arrivals`'s distance guard rather than here.
    """
    conn.execute(
        """
        CREATE TEMP TABLE interior_crossings ON COMMIT DROP AS
        SELECT DISTINCT ON (pp.trip_id, sf.route_direction_feed_id, sf.stop_sequence)
            pp.trip_id, sf.route_direction_feed_id, sf.stop_sequence,
            pp.prev_ping_at AS t1, pp.prev_fraction AS fraction1, pp.prev_geom AS geom1,
            pp.ping_at AS t2, pp.fraction AS fraction2, pp.geom AS geom2,
            sf.fraction AS stop_fraction, sf.stop_geom
        FROM ping_progress pp
        JOIN stop_fractions sf
          ON sf.route_direction_feed_id = pp.route_direction_feed_id
         AND sf.fraction <= pp.running_max_fraction
        WHERE pp.prev_fraction IS NOT NULL
        ORDER BY pp.trip_id, sf.route_direction_feed_id, sf.stop_sequence,
                 pp.ping_at ASC
        """
    )
    conn.execute("ANALYZE interior_crossings")


def _load_late_crossings(conn: psycopg.Connection) -> None:
    """Extrapolate forward for stops beyond the trip's last-reached fraction.

    The interior method can never reach these (no ping's running max
    exceeds their fraction), so they need their own source: the trip's
    last two pings, used as an extrapolation line the same way the first
    two are reused for an early stop in `interior_crossings`.
    """
    conn.execute(
        """
        CREATE TEMP TABLE trip_tail ON COMMIT DROP AS
        SELECT trip_id, route_direction_feed_id,
            MAX(fraction) AS max_fraction,
            (array_agg(ping_at ORDER BY ping_at DESC))[1] AS last_at,
            (array_agg(fraction ORDER BY ping_at DESC))[1] AS last_fraction,
            (array_agg(geom ORDER BY ping_at DESC))[1] AS last_geom,
            (array_agg(ping_at ORDER BY ping_at DESC))[2] AS prev_at,
            (array_agg(fraction ORDER BY ping_at DESC))[2] AS prev_fraction,
            (array_agg(geom ORDER BY ping_at DESC))[2] AS prev_geom
        FROM ping_progress
        GROUP BY trip_id, route_direction_feed_id
        HAVING COUNT(*) >= 2
        """
    )
    conn.execute("ANALYZE trip_tail")
    conn.execute(
        """
        CREATE TEMP TABLE late_crossings ON COMMIT DROP AS
        SELECT tt.trip_id, sf.route_direction_feed_id, sf.stop_sequence,
            tt.prev_at AS t1, tt.prev_fraction AS fraction1, tt.prev_geom AS geom1,
            tt.last_at AS t2, tt.last_fraction AS fraction2, tt.last_geom AS geom2,
            sf.fraction AS stop_fraction, sf.stop_geom
        FROM trip_tail tt
        JOIN stop_fractions sf
          ON sf.route_direction_feed_id = tt.route_direction_feed_id
         AND sf.fraction > tt.max_fraction
        """
    )
    conn.execute("ANALYZE late_crossings")


def _load_safe_crossings(conn: psycopg.Connection) -> None:
    """Combine interior and late crossings, bounding the extrapolation weight.

    Split into its own step, computed *before* any timestamp arithmetic
    touches these rows: `_MAX_EXTRAPOLATION_FACTOR` has to be enforced on
    the plain floating-point weight first, because computing
    `t1 + (t2-t1)*weight` for a row with a pathological weight is what
    caused a real `timestamp out of range` error while building this
    module (see `_MAX_EXTRAPOLATION_FACTOR`'s own comment) -- filtering
    afterward would be too late, since the bad arithmetic already ran.
    """
    conn.execute(
        """
        CREATE TEMP TABLE safe_crossings ON COMMIT DROP AS
        SELECT *, (stop_fraction - fraction1) / (fraction2 - fraction1) AS weight
        FROM (
            SELECT * FROM interior_crossings
            UNION ALL
            SELECT * FROM late_crossings
        ) x
        WHERE fraction2 != fraction1
          AND ABS((stop_fraction - fraction1) / (fraction2 - fraction1))
              <= %(max_extrapolation)s
        """,
        {"max_extrapolation": _MAX_EXTRAPOLATION_FACTOR},
    )
    conn.execute("ANALYZE safe_crossings")


def _load_stop_arrivals(conn: psycopg.Connection) -> None:
    """Filter detours and implausible extrapolations, then interpolate.

    The two boundary-extrapolation cases (a stop before the trip's first
    ping, or after its last) are told apart from normal interior brackets
    by comparing the stop's fraction to the bracket's own two fractions:
    a genuine interior bracket always has fraction1 < stop_fraction <=
    fraction2. Whenever that's not the case, the extrapolation is only
    kept if *both*:
    - the stop is within `_BOUNDARY_EXTRAPOLATION_MAX_M` of whichever
      bracket ping is closer to it in time (geom1 for an early stop,
      geom2 for a late one), and
    - the resulting arrival time is within `_MAX_EXTRAPOLATION_SECONDS`
      of that same reference ping's own timestamp.
    Otherwise it's dropped rather than extrapolated across an implausibly
    large gap. Interior brackets need neither check: a real bracket's
    weight is always in [0, 1] by construction, so no extrapolation ever
    happens there regardless of how large the ping gap is.
    """
    conn.execute(
        """
        INSERT INTO diamond.stop_arrivals
            (trip_id, route_direction_feed_id, stop_sequence, arrival_time,
             ping_gap_seconds, confidence)
        SELECT
            sc.trip_id, sc.route_direction_feed_id, sc.stop_sequence,
            sc.t1 + (sc.t2 - sc.t1) * sc.weight,
            EXTRACT(EPOCH FROM (sc.t2 - sc.t1))::integer,
            CASE
                WHEN EXTRACT(EPOCH FROM (sc.t2 - sc.t1)) <= %(high)s THEN 'high'
                WHEN EXTRACT(EPOCH FROM (sc.t2 - sc.t1)) <= %(medium)s THEN 'medium'
                ELSE 'low'
            END
        FROM safe_crossings sc
        JOIN route_lines rl USING (route_direction_feed_id)
        WHERE ST_Distance(sc.geom1::geography, rl.line::geography) <= %(cross_track)s
          AND ST_Distance(sc.geom2::geography, rl.line::geography) <= %(cross_track)s
          AND (
              (sc.fraction1 < sc.stop_fraction AND sc.stop_fraction <= sc.fraction2)
              OR (
                  ST_Distance(
                      sc.stop_geom::geography,
                      (CASE WHEN sc.stop_fraction < sc.fraction1
                            THEN sc.geom1 ELSE sc.geom2 END)::geography
                  ) <= %(boundary)s
                  AND ABS(EXTRACT(EPOCH FROM (
                      (sc.t1 + (sc.t2 - sc.t1) * sc.weight)
                      - (CASE WHEN sc.stop_fraction < sc.fraction1
                              THEN sc.t1 ELSE sc.t2 END)
                  ))) <= %(max_extrapolation_seconds)s
              )
          )
        """,
        {
            "high": _HIGH_CONFIDENCE_GAP_S,
            "medium": _MEDIUM_CONFIDENCE_GAP_S,
            "cross_track": _CROSS_TRACK_THRESHOLD_M,
            "boundary": _BOUNDARY_EXTRAPOLATION_MAX_M,
            "max_extrapolation_seconds": _MAX_EXTRAPOLATION_SECONDS,
        },
    )


def build(year: int, month: int) -> None:
    """Build diamond.stop_arrivals from scratch for one calendar month.

    Drops and recreates the `diamond` schema, then computes every valid
    trip's stop arrival times from `gold.trips`, `gold.route_direction_*`,
    `gold.stops`, and `gold.trip_positions` (AVL-origin rows only).
    Trips with no matched GTFS route (`route_direction_feed_id IS NULL`)
    are skipped -- there's no route shape to project onto. One-shot for
    the given month, matching gold's own current scope.

    Args:
        year (int): Calendar year to build diamond for.
        month (int): Calendar month to build diamond for.

    """
    start, end = _period_bounds(year, month)

    def _create_schema() -> None:
        conn.execute("DROP SCHEMA IF EXISTS diamond CASCADE")
        conn.execute(_DDL)

    with get_connection() as conn, conn.transaction():
        conn.execute("SET LOCAL work_mem = '512MB'")
        conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")

        _step("drop/create schema", _create_schema)
        _step("clean_pings", lambda: _load_clean_pings(conn, start, end))
        _step("route_lines", lambda: _load_route_lines(conn))
        _step("stop_fractions", lambda: _load_stop_fractions(conn))
        _step("ping_progress", lambda: _load_ping_progress(conn))
        _step("interior_crossings", lambda: _load_interior_crossings(conn))
        _step("late_crossings", lambda: _load_late_crossings(conn))
        _step("safe_crossings", lambda: _load_safe_crossings(conn))
        _step("stop_arrivals", lambda: _load_stop_arrivals(conn))

        def _create_indexes() -> None:
            for statement in _INDEXES:
                conn.execute(statement)

        _step("indexes", _create_indexes)
