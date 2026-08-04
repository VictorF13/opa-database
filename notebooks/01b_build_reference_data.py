"""Stage 0-2: route shapes, GTFS feed resolution, and the reference speed model.

These are shared inputs 01c and 01d both read from scratch.*.

Persisted into the `scratch` schema (not TEMP tables) so 01c/01d can run as
separate processes without rebuilding this every time. Each of the three
tables is skipped on rerun if it already exists (see build_if_missing in
_scoring_lib.py) -- to force a rebuild, manually
`DROP TABLE scratch.<name>`, or `DROP SCHEMA scratch CASCADE` to reset
everything Stage 0-2 built.

Run with: uv run notebooks/01b_build_reference_data.py
(after 01a_build_avl_indexes.py, though this stage doesn't touch avl_pings
so it doesn't strictly need the indexes -- 01c/01d do.)
"""

from __future__ import annotations

import pandas as pd
from _scoring_lib import (
    OUTPUT_DIR,
    PARAMS,
    build_if_missing,
    open_connection,
    write_parquet,
)


def build_route_shape_geoms(conn) -> None:  # noqa: ANN001
    """Stage 0: one LineString per (feed_version_date, line_number, shape_id).

    Restricted to routes that actually appear in 2023 AFC data (line_number),
    so the geometry work is bounded to what's actually needed downstream.
    """
    build_if_missing(
        conn,
        "scratch.route_shape_geoms",
        [
            (
                """
                CREATE TABLE scratch.route_shape_geoms AS
                WITH afc_routes AS (
                    SELECT DISTINCT line_number
                    FROM silver.afc_boardings
                    WHERE trip_opened_at >= %(start)s AND trip_opened_at < %(end)s
                ),
                shape_lines AS (
                    SELECT
                        shape_id,
                        feed_version_date,
                        ST_MakeLine(geom ORDER BY shape_pt_sequence) AS line_geom
                    FROM silver.gtfs_shapes
                    GROUP BY shape_id, feed_version_date
                ),
                route_trip_shapes AS (
                    SELECT DISTINCT
                        t.feed_version_date,
                        r.route_short_name AS line_number,
                        t.shape_id,
                        t.direction_id
                    FROM silver.gtfs_trips t
                    JOIN silver.gtfs_routes r
                      ON r.feed_version_date = t.feed_version_date
                     AND r.route_id = t.route_id
                    JOIN afc_routes a ON a.line_number = r.route_short_name
                    WHERE t.shape_id IS NOT NULL
                )
                SELECT
                    rts.feed_version_date,
                    rts.line_number,
                    rts.shape_id,
                    rts.direction_id,
                    sl.line_geom,
                    ST_Length(sl.line_geom::geography) AS shape_length_m,
                    -- Fortaleza sits in UTM zone 24S (EPSG:32724); reprojecting
                    -- here lets Stage 3 use fast planar ST_Distance/
                    -- ST_LineLocatePoint in real meters instead of ::geography
                    -- casts, which do (much slower) geodetic math. Simplifying
                    -- to a 10m tolerance -- well under GPS accuracy -- cuts some
                    -- routes from ~900+ vertices down to a handful. Point-to-line
                    -- distance is roughly O(vertex count) per call, and this was
                    -- run per ping (not per trip), which is what made a single
                    -- month take 3+ hours instead of minutes.
                    ST_Simplify(
                        ST_Transform(sl.line_geom, 32724), 10
                    ) AS line_geom_proj
                FROM route_trip_shapes rts
                JOIN shape_lines sl
                  ON sl.feed_version_date = rts.feed_version_date
                 AND sl.shape_id = rts.shape_id
                """,
                PARAMS,
            ),
            (
                "CREATE INDEX ON scratch.route_shape_geoms "
                "(feed_version_date, line_number, shape_id)",
                None,
            ),
        ],
    )


def resolve_trip_feeds(conn) -> None:  # noqa: ANN001
    """Stage 1: pick a feed_version_date per (line_number, trip date).

    Search backward through every earlier feed (nearest first) for one that
    actually contains this route; if none, search forward through every later
    feed (nearest first); if the route never appears anywhere, leave it
    unresolved -- that trip won't get GTFS-dependent numbers later.
    """
    build_if_missing(
        conn,
        "scratch.trip_feed_resolution",
        [
            (
                """
                CREATE TABLE scratch.trip_feed_resolution AS
                WITH afc_trip_dates AS (
                    SELECT DISTINCT line_number, trip_opened_at::date AS trip_date
                    FROM silver.afc_boardings
                    WHERE trip_opened_at >= %(start)s AND trip_opened_at < %(end)s
                ),
                route_feeds AS (
                    SELECT DISTINCT line_number, feed_version_date
                    FROM scratch.route_shape_geoms
                )
                SELECT
                    d.line_number,
                    d.trip_date,
                    COALESCE(
                        (SELECT MAX(f.feed_version_date) FROM route_feeds f
                         WHERE f.line_number = d.line_number
                           AND f.feed_version_date < d.trip_date),
                        (SELECT MIN(f.feed_version_date) FROM route_feeds f
                         WHERE f.line_number = d.line_number
                           AND f.feed_version_date >= d.trip_date)
                    ) AS resolved_feed_version_date
                FROM afc_trip_dates d
                """,
                PARAMS,
            ),
            (
                "CREATE INDEX ON scratch.trip_feed_resolution (line_number, trip_date)",
                None,
            ),
        ],
    )


def build_reference_model(conn) -> None:  # noqa: ANN001
    """Stage 2: raw per-(trip x shape) duration/implied-speed sample table.

    Grain is every distinct 2023 trip (network-wide, every vehicle) crossed
    with every GTFS shape candidate for its resolved feed. Pooled into a
    (line_number, local_hour) reference distribution via PERCENT_RANK(), with
    a fallback to the nearest hour (same line_number) when a bucket has under
    100 distinct trips/year. The three intermediate tables here (unqualified
    names) are TEMP -- only the final scratch.trip_shape_samples_scored needs
    to survive past this script, since that's what 01c/01d read.
    """
    build_if_missing(
        conn,
        "scratch.trip_shape_samples_scored",
        [
            ("DROP TABLE IF EXISTS trip_shape_samples", None),
            (
                """
                CREATE TEMP TABLE trip_shape_samples AS
                WITH trips AS (
                    SELECT DISTINCT
                        vehicle_number, line_number, line_shift,
                        line_opened_at, line_closed_at,
                        trip_opened_at, trip_closed_at
                    FROM silver.afc_boardings
                    WHERE trip_opened_at >= %(start)s AND trip_opened_at < %(end)s
                ),
                trips_resolved AS (
                    SELECT
                        t.*,
                        EXTRACT(
                            EPOCH FROM (t.trip_closed_at - t.trip_opened_at)
                        ) AS duration_sec,
                        EXTRACT(
                            HOUR FROM
                                (t.trip_opened_at AT TIME ZONE 'America/Fortaleza')
                        )::int AS local_hour,
                        r.resolved_feed_version_date
                    FROM trips t
                    JOIN scratch.trip_feed_resolution r
                      ON r.line_number = t.line_number
                     AND r.trip_date = t.trip_opened_at::date
                )
                SELECT
                    tr.vehicle_number, tr.line_number, tr.line_shift,
                    tr.line_opened_at, tr.line_closed_at,
                    tr.trip_opened_at, tr.trip_closed_at,
                    tr.duration_sec, tr.local_hour,
                    tr.resolved_feed_version_date,
                    g.shape_id, g.direction_id, g.shape_length_m,
                    (g.shape_length_m / NULLIF(tr.duration_sec, 0)) * 3.6
                        AS implied_speed_kmh
                FROM trips_resolved tr
                JOIN scratch.route_shape_geoms g
                  ON g.feed_version_date = tr.resolved_feed_version_date
                 AND g.line_number = tr.line_number
                WHERE tr.resolved_feed_version_date IS NOT NULL
                  AND tr.duration_sec > 0
                """,
                PARAMS,
            ),
            ("CREATE INDEX ON trip_shape_samples (vehicle_number)", None),
            ("CREATE INDEX ON trip_shape_samples (line_number, local_hour)", None),
            ("DROP TABLE IF EXISTS hour_bucket_counts", None),
            (
                """
                CREATE TEMP TABLE hour_bucket_counts AS
                SELECT
                    line_number,
                    local_hour,
                    COUNT(DISTINCT (trip_opened_at, trip_closed_at)) AS trip_count
                FROM trip_shape_samples
                GROUP BY line_number, local_hour
                """,
                None,
            ),
            ("DROP TABLE IF EXISTS hour_fallback_map", None),
            (
                """
                CREATE TEMP TABLE hour_fallback_map AS
                SELECT DISTINCT ON (b.line_number, b.local_hour)
                    b.line_number,
                    b.local_hour,
                    CASE
                        WHEN b.trip_count >= 100 THEN b.local_hour
                        ELSE v.local_hour
                    END AS effective_hour
                FROM hour_bucket_counts b
                LEFT JOIN hour_bucket_counts v
                  ON v.line_number = b.line_number
                 AND v.trip_count >= 100
                 AND b.trip_count < 100
                ORDER BY
                    b.line_number, b.local_hour,
                    LEAST(
                        ABS(b.local_hour - v.local_hour),
                        24 - ABS(b.local_hour - v.local_hour)
                    ) ASC NULLS LAST
                """,
                None,
            ),
            ("CREATE INDEX ON hour_fallback_map (line_number, local_hour)", None),
            (
                """
                CREATE TABLE scratch.trip_shape_samples_scored AS
                SELECT
                    s.*,
                    hbc.trip_count,
                    m.effective_hour,
                    PERCENT_RANK() OVER (
                        PARTITION BY s.line_number, m.effective_hour
                        ORDER BY s.implied_speed_kmh
                    ) AS speed_percentile
                FROM trip_shape_samples s
                JOIN hour_bucket_counts hbc
                  ON hbc.line_number = s.line_number AND hbc.local_hour = s.local_hour
                JOIN hour_fallback_map m
                  ON m.line_number = s.line_number AND m.local_hour = s.local_hour
                """,
                None,
            ),
            (
                "CREATE INDEX ON scratch.trip_shape_samples_scored (vehicle_number)",
                None,
            ),
            (
                "CREATE INDEX ON scratch.trip_shape_samples_scored "
                "(resolved_feed_version_date, line_number, shape_id)",
                None,
            ),
            (
                "CREATE INDEX ON scratch.trip_shape_samples_scored (trip_opened_at)",
                None,
            ),
        ],
    )


def main() -> None:
    """Build Stage 0-2 (skipping tables that already exist), export deliverable #1."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = open_connection()

    print("Stage 0: route shape geometries")
    build_route_shape_geoms(conn)

    print("\nStage 1: feed resolution")
    resolve_trip_feeds(conn)

    print("\nStage 2: reference speed/duration model")
    build_reference_model(conn)

    reference_df = pd.read_sql("SELECT * FROM scratch.trip_shape_samples_scored", conn)
    write_parquet(reference_df, OUTPUT_DIR / "route_hour_speed_reference_2023.parquet")

    conn.close()


if __name__ == "__main__":
    main()
