"""Shared helpers for the vehicle-mapping scoring pipeline (01a-01d).

Not a standalone script -- imported by 01a_build_avl_indexes.py,
01b_build_reference_data.py, 01c_score_device_mapping.py, and
01d_score_vehicle_mapping.py.

Intermediate results live in the `scratch` Postgres schema as regular
(non-temp) tables, committed as they're built. That's specifically so a later
stage -- or a rerun of the same stage after a crash/cancel -- can pick up
where a previous run left off instead of recomputing everything: 01a's
indexes are permanent once built, 01b's tables are skipped on rerun if they
already exist, and 01c/01d resume from whichever months are already present
instead of restarting the whole year. See
/home/victor/.claude/plans/hazy-mixing-hellman.md for the full design.
"""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from typing import TYPE_CHECKING, LiteralString

import pandas as pd

from opa_database.loaders.silver import get_connection as _get_raw_connection

if TYPE_CHECKING:
    import psycopg

YEAR_START = dt.date(2023, 1, 1)
YEAR_END = dt.date(2024, 1, 1)
PARAMS = {"start": YEAR_START, "end": YEAR_END}

SCRATCH_SCHEMA = "scratch"

NOTEBOOK_DIR = Path("notebooks") if Path("notebooks").is_dir() else Path()
OUTPUT_DIR = NOTEBOOK_DIR / "data"


def _month_ranges(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Split [start, end) into calendar-month chunks.

    Stage 3's candidate-trips x avl_pings join fans out by GTFS shape variant
    before the GROUP BY collapses it, so its true intermediate size approaches
    the full avl_pings row count x up to ~3 -- multiple billions of rows in one
    shot, which is what blew through 76GB of temp-file spill and nearly took the
    host's root disk to zero. Chunking by month (matching how avl_pings is
    already partitioned) cuts that ~12x per query, and lets each month commit
    (and be skipped on a later resume) independently.
    """
    months_per_year = 12
    ranges = []
    cursor = start
    while cursor < end:
        nxt = dt.date(
            cursor.year + (cursor.month == months_per_year),
            cursor.month % months_per_year + 1,
            1,
        )
        ranges.append((cursor, min(nxt, end)))
        cursor = nxt
    return ranges


MONTH_RANGES = _month_ranges(YEAR_START, YEAR_END)

# Empirically measured on a January sample (query time, devices), before
# adding the (vehicle_number, trip_opened_at) composite index on
# scratch.trip_shape_samples_scored (see 01b/index history):
#   20 -> 8.3s (0.42s/device), 40 -> 13.2s (0.33s/device, best),
#   60 -> 21.3s (0.36s/device), 80 -> 40.9s (0.51s/device),
#   100 -> 150.4s (1.50s/device -- a cliff, not a smooth curve).
# That cliff came from the planner falling back to an expensive full-month
# bitmap scan of trip_shape_samples_scored once a batch got large enough to
# tip its cost estimate. The composite index removed that expensive path
# entirely -- re-measured afterward (under contention from a concurrently
# running index build, so these are conservative) at 100 -> 0.57s/device,
# now the *best* of the sizes tested, no cliff in sight up to 100. Bumped
# from 50 accordingly; fewer, larger batches means less repeated per-query
# fixed overhead with no more super-linear penalty to offset it.
DEVICE_BATCH_SIZE = 100


def open_connection(work_mem: LiteralString = "4GB") -> psycopg.Connection:
    """Open a silver connection tuned for this pipeline's heavy aggregations.

    work_mem defaults to a generous value -- the stock 4MB default turned out
    to be the same kind of bottleneck the stock 64MB maintenance_work_mem was
    for the avl_pings index build, on a host confirmed to have 24 cores and
    ~53GB free RAM headroom. Ensures the scratch schema exists.

    Args:
        work_mem: Postgres work_mem for this session, e.g. "4GB".

    Returns:
        psycopg.Connection: open connection, autocommit off (caller commits
            explicitly after each durable step -- see build_if_missing and
            score_candidate_mapping). Caller is responsible for closing it.

    """
    conn = _get_raw_connection()
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCRATCH_SCHEMA}")
        # work_mem is caller-controlled config (never user input); this project's
        # own convention (see the S608 comments below) is to note that explicitly
        # rather than silence the linter blindly.
        cur.execute(f"SET work_mem = '{work_mem}'")
        # docker-compose.yml caps /dev/shm at 1GB (shm_size: "1gb"). A parallel
        # plan against a work_mem this large can ask for a shared-memory segment
        # per worker that blows past that container limit and fails immediately
        # with psycopg.errors.DiskFull -- disabling parallel workers avoids that
        # without needing a container restart. Stage 3 is disk/hash-aggregate
        # bound anyway, not much parallelism upside for this workload.
        cur.execute("SET max_parallel_workers_per_gather = 0")
    conn.commit()
    return conn


def table_exists(conn: psycopg.Connection, qualified_name: str) -> bool:
    """Check whether a table/relation already exists, in any schema."""
    return bool(
        pd.read_sql(
            "SELECT to_regclass(%(name)s) IS NOT NULL AS exists",
            conn,
            params={"name": qualified_name},
        )["exists"].iloc[0]
    )


def row_count(conn: psycopg.Connection, qualified_name: str) -> int:
    """Count rows in a table.

    qualified_name is always a hardcoded literal from this codebase, never
    user input, despite the f-string.
    """
    return int(
        pd.read_sql(
            f"SELECT count(*) AS n FROM {qualified_name}",  # noqa: S608
            conn,
        )["n"].iloc[0]
    )


def build_if_missing(
    conn: psycopg.Connection,
    table: LiteralString,
    statements: list[tuple[LiteralString, dict | None]],
) -> None:
    """Build `table` via `statements` unless it already exists, then commit.

    This is what makes 01b_build_reference_data.py cheap/safe to rerun: each
    of its three tables is skipped (just reports existing row count) if
    already present. To force a rebuild, manually
    `DROP TABLE <table>` first, or `DROP SCHEMA scratch CASCADE` to reset
    everything Stage 0-2 built.

    Args:
        conn: Open silver connection.
        table: Fully schema-qualified table this set of statements produces
            (e.g. "scratch.route_shape_geoms") -- checked for existence and
            used for the final row-count report.
        statements: (sql, params) pairs run in order in one cursor if `table`
            doesn't exist yet.

    """
    if table_exists(conn, table):
        print(f"  {table} already exists, skipping ({row_count(conn, table)} rows)")
        return
    t0 = time.time()
    with conn.cursor() as cur:
        for sql, params in statements:
            cur.execute(sql, params)
    conn.commit()
    print(f"  {table}: built ({time.time() - t0:.1f}s, {row_count(conn, table)} rows)")


def write_parquet(df: pd.DataFrame, path: Path) -> None:
    """Write `df` to `path` and print row count / size / elapsed time."""
    t0 = time.time()
    df.to_parquet(path, index=False)
    size_mb = path.stat().st_size / 1e6
    print(
        f"  wrote {len(df)} rows to {path} ({size_mb:.1f} MB, {time.time() - t0:.1f}s)"
    )


_CANDIDATE_SCORING_SELECT = """
    scored_pings AS (
        -- Row-level (not yet aggregated) distance/line-fraction per ping, joined
        -- through route_shape_geoms's *projected* line (UTM 24S / EPSG:32724,
        -- meters, simplified to 10m tolerance -- see 01b). Planar ST_Distance
        -- there skips both ::geography's slow geodetic math and the O(vertex
        -- count) cost of the original ~900-vertex, unsimplified, degree-CRS
        -- line -- that combination alone took a single month from minutes to
        -- multiple hours.
        SELECT
            ct.ct_row_id,
            p.metric_timestamp,
            ST_Transform(p.geom, 32724) AS geom_proj,
            g.line_geom_proj,
            ST_Distance(ST_Transform(p.geom, 32724), g.line_geom_proj)
                AS dist_to_line_m,
            ST_LineLocatePoint(g.line_geom_proj, ST_Transform(p.geom, 32724))
                AS line_frac
        FROM candidate_trips ct
        JOIN scratch.route_shape_geoms g
          ON g.feed_version_date = ct.resolved_feed_version_date
         AND g.line_number = ct.line_number
         AND g.shape_id = ct.shape_id
        LEFT JOIN silver.avl_pings p
          ON p.{avl_join_col} = ct.{candidate_id_col}
         AND p.metric_timestamp BETWEEN ct.trip_opened_at AND ct.trip_closed_at
         -- redundant given the BETWEEN above (every trip_opened_at/trip_closed_at
         -- in candidate_trips already falls in this padded window), but stated as
         -- a constant so the planner can actually prune avl_pings partitions with
         -- it -- BETWEEN alone is a per-row bound correlated to another table,
         -- which Postgres can't use for partition pruning, so without this every
         -- trip's ping lookup was probing all 12 monthly partitions instead of
         -- the ~1-2 that could ever match.
         AND p.metric_timestamp >= %(ping_window_start)s
         AND p.metric_timestamp < %(ping_window_end)s
    )
    SELECT
        any_value(ct.{candidate_id_col}) AS {candidate_id_col},
        any_value(ct.vehicle_number) AS vehicle_number,
        any_value(ct.line_number) AS line_number,
        any_value(ct.line_shift) AS line_shift,
        any_value(ct.trip_opened_at) AS trip_opened_at,
        any_value(ct.trip_closed_at) AS trip_closed_at,
        any_value(ct.resolved_feed_version_date) AS resolved_feed_version_date,
        any_value(ct.shape_id) AS shape_id,
        any_value(ct.direction_id) AS direction_id,
        any_value(ct.duration_sec) AS duration_sec,
        any_value(ct.implied_speed_kmh) AS implied_speed_kmh,
        any_value(ct.speed_percentile) AS speed_percentile,
        any_value(ct.trip_count) AS trip_count,
        COUNT(sp.metric_timestamp) AS n_pings_in_window,
        AVG(sp.dist_to_line_m) AS avg_dist_to_line_m,
        CORR(EXTRACT(EPOCH FROM sp.metric_timestamp), sp.line_frac) AS progress_corr,
        ST_Distance(
            (array_agg(sp.geom_proj ORDER BY sp.metric_timestamp ASC))[1],
            ST_StartPoint(any_value(sp.line_geom_proj))
        ) AS start_proximity_m,
        ST_Distance(
            (array_agg(sp.geom_proj ORDER BY sp.metric_timestamp DESC))[1],
            ST_EndPoint(any_value(sp.line_geom_proj))
        ) AS end_proximity_m
    FROM candidate_trips ct
    JOIN scored_pings sp ON sp.ct_row_id = ct.ct_row_id
    -- Grouping by this single integer surrogate key (assigned once when
    -- candidate_trips is materialized below) instead of the original 13-column
    -- mixed text/timestamp composite key is what took the final sort from ~8s to
    -- ~1-3s: comparing one int is far cheaper than comparing 13 columns per pair,
    -- especially with several text columns in the mix. any_value() above is safe
    -- because ct_row_id uniquely identifies exactly one candidate_trips row, so
    -- every other ct.* column is trivially constant within its group.
    GROUP BY ct.ct_row_id
"""


def score_candidate_mapping(
    conn: psycopg.Connection,
    table_name: LiteralString,
    candidates_cte: LiteralString,
    candidate_id_col: LiteralString,
    avl_join_col: LiteralString,
) -> pd.DataFrame:
    """Run a Stage 3 candidate-scoring query one (month, device batch) at a time.

    Commits after each batch and skips (month, candidate) pairs already present
    in `table_name` on entry -- a killed/crashed/cancelled run resumes by just
    rerunning the same script (01c or 01d), redoing at most one in-flight batch
    instead of a whole month or the whole year.

    Batching by DEVICE_BATCH_SIZE (not just by month) is the fix for a real
    measured problem: querying all ~1900 candidates in one statement degraded
    super-linearly (20 candidates: 8.3s: 40: 13.2s (best): 60: 21.3s: 80: 40.9s:
    100: 150.4s -- a cliff, not a smooth curve), most likely because the working
    set stops fitting comfortably in memory/cache past a few dozen candidates.
    Many small queries in the flat part of that curve beat one giant query by
    roughly an order of magnitude in total wall-clock time.

    Args:
        conn: Open silver connection (see open_connection).
        table_name: Fully schema-qualified table to build/resume, e.g.
            "scratch.device_mapping_trip_scores".
        candidates_cte: Full SELECT for the candidate list (vehicle_number plus
            the AVL-side id column) -- hardcoded per caller, not user input.
            Run once (candidate lists are small, ~1900-2000 rows) rather than
            per batch.
        candidate_id_col: `device_id` or `vehicle_id`.
        avl_join_col: Column on silver.avl_pings to join candidate_trips against
            (same as candidate_id_col here, kept separate for clarity/reuse).

    Returns:
        pd.DataFrame: full table after all (month, candidate) pairs are present.

    """
    candidate_id_sql_type = "integer" if candidate_id_col == "vehicle_id" else "text"
    select = _CANDIDATE_SCORING_SELECT.format(
        candidate_id_col=candidate_id_col, avl_join_col=avl_join_col
    )
    # select/candidate_id_col/avl_join_col/table_name are built from hardcoded
    # templates with only literal strings substituted in (never user input), so
    # the f-strings below are not an injection vector despite the S608 pattern.
    # candidates now comes from a per-batch unnest(...) of two parameter arrays
    # (real bind params, not string-interpolated), not from candidates_cte
    # directly -- candidates_cte is only used once below to fetch the full list.
    body = f"""
        WITH candidates AS MATERIALIZED (
            SELECT
                vehicle_number,
                candidate_id_raw::{candidate_id_sql_type} AS {candidate_id_col}
            FROM unnest(
                %(batch_vehicle_numbers)s::text[], %(batch_candidate_ids)s::text[]
            ) AS c(vehicle_number, candidate_id_raw)
        ),
        -- MATERIALIZED forces Postgres to compute this small, well-filtered set
        -- (this batch x this month's samples) BEFORE joining to route_shape_geoms.
        -- Without it, on the full candidate set, the planner badly misestimated
        -- this join's selectivity (2,452 estimated vs 1.79 MILLION actual rows)
        -- and chose to scan all 33,169 shapes for the *entire year* as the outer
        -- loop, filtering down to actual candidates only at the very end.
        candidate_trips AS MATERIALIZED (
            -- ct_row_id: single-integer surrogate key, see the comment on the
            -- final GROUP BY in _CANDIDATE_SCORING_SELECT for why.
            SELECT ROW_NUMBER() OVER () AS ct_row_id, c.{candidate_id_col}, s.*
            FROM candidates c
            JOIN scratch.trip_shape_samples_scored s
              ON s.vehicle_number = c.vehicle_number
             AND s.trip_opened_at >= %(month_start)s
             AND s.trip_opened_at < %(month_end)s
        ),
        {select}
        """  # noqa: S608

    all_candidates = pd.read_sql(candidates_cte, conn)
    print(f"  {len(all_candidates)} total candidates")

    already_exists = table_exists(conn, table_name)

    for month_start, month_end in MONTH_RANGES:
        done_ids: set = set()
        if already_exists:
            done = pd.read_sql(
                f"SELECT DISTINCT {candidate_id_col} FROM {table_name} "  # noqa: S608
                "WHERE trip_opened_at >= %(s)s AND trip_opened_at < %(e)s",
                conn,
                params={"s": month_start, "e": month_end},
            )
            done_ids = set(done[candidate_id_col])

        remaining = all_candidates.loc[~all_candidates[candidate_id_col].isin(done_ids)]
        if len(remaining) == 0:
            print(f"  {month_start:%Y-%m}: already fully done, skipping")
            continue
        if done_ids:
            print(
                f"  {month_start:%Y-%m}: {len(done_ids)} candidates already done, "
                f"{len(remaining)} remaining"
            )

        n_batches = -(-len(remaining) // DEVICE_BATCH_SIZE)  # ceil division
        for i in range(n_batches):
            batch = remaining.iloc[i * DEVICE_BATCH_SIZE : (i + 1) * DEVICE_BATCH_SIZE]
            t0 = time.time()
            verb = (
                f"INSERT INTO {table_name}"
                if already_exists
                else f"CREATE TABLE {table_name} AS"
            )
            with conn.cursor() as cur:
                cur.execute(
                    f"{verb}\n{body}",
                    {
                        "batch_vehicle_numbers": batch["vehicle_number"]
                        .astype(str)
                        .tolist(),
                        "batch_candidate_ids": batch[candidate_id_col]
                        .astype(str)
                        .tolist(),
                        "month_start": month_start,
                        "month_end": month_end,
                        # padded a day either side so a trip starting just before
                        # midnight on the month boundary still gets its pings that
                        # land just after -- avl_pings partitions are monthly, so
                        # this still prunes to ~2-3 partitions instead of all 12.
                        "ping_window_start": month_start - dt.timedelta(days=1),
                        "ping_window_end": month_end + dt.timedelta(days=1),
                    },
                )
            conn.commit()
            already_exists = True
            print(
                f"  {month_start:%Y-%m} batch {i + 1}/{n_batches}: "
                f"done ({time.time() - t0:.1f}s, {len(batch)} candidates)"
            )

    print(f"  {row_count(conn, table_name)} rows total")
    return pd.read_sql(f"SELECT * FROM {table_name}", conn)  # noqa: S608
