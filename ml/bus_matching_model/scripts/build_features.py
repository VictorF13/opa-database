"""Build the full month's per-`(bus, device, date)` Tier 1+2 feature table.

Writes one Parquet per date to `artifacts/features_v2/`, the input to
both the day-level model and (aggregated) the pair-level model.

**Parallel by date.** Dates are fully independent here -- each one pulls
its own AVL positions, trips, and fares and never looks at another --
so this forks one worker per date across a process pool. The earlier
serial build of the smaller Tier 1 set took ~100 minutes; this
parallelizes that same work while also computing 8 more features.

**Excludes no-AVL companies** (COOTRAPS, Fretcar -- confirmed live that
100% of their devices never ping, see notebook 06). Skipping their
~5,400 bus-dates up front is both faster and avoids generating feature
rows that could only ever be noise.

Writes to `features_v2/` rather than overwriting `features/`, so the
current production results stay reproducible while the new pipeline is
validated alongside them.

Usage::

    uv run ml/bus_matching_model/scripts/build_features.py
    uv run ml/bus_matching_model/scripts/build_features.py --dates 2023-11-01
    uv run ml/bus_matching_model/scripts/build_features.py --workers 4
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg

_APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from features import (  # noqa: E402
    DAY_FEATURE_NAMES,
    TripPositions,
    compute_pair_day_features,
    select_sample_trips,
)
from gtfs_cache import build_shape_cache  # noqa: E402

from opa_database.config import settings  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "artifacts" / "features_v2"

_NO_AVL_COMPANIES_SQL = """
    WITH company_ping_rates AS (
        SELECT
            d.company,
            count(DISTINCT d.device_id) AS n_devices,
            count(DISTINCT d.device_id) FILTER (
                WHERE EXISTS (
                    SELECT 1 FROM silver.avl_pings_y2023m11 p
                    WHERE p.device_id = d.device_id
                )
            ) AS n_pinging
        FROM silver.dictionary_device d
        WHERE d.device_id IS NOT NULL
        GROUP BY d.company
    ),
    no_avl AS (
        SELECT company FROM company_ping_rates
        WHERE n_devices > 0 AND n_pinging = 0
    ),
    normalized AS (
        SELECT regexp_replace(vehicle_number, '[^0-9]', '', 'g') AS digits
        FROM silver.dictionary_device
        WHERE company IN (SELECT company FROM no_avl)
    )
    SELECT DISTINCT
        CASE WHEN length(digits) < 5 THEN lpad(digits, 5, '0') ELSE digits END AS bus_id
    FROM normalized
    WHERE digits <> '';
"""

_TRIPS_SQL = """
    SELECT
        f.trip_id, f.bus_id, f.route_id, t.route_direction,
        extract(epoch FROM f.trip_start_timestamp)::float8 AS trip_start_timestamp,
        extract(epoch FROM f.trip_end_timestamp)::float8 AS trip_end_timestamp,
        f.gtfs_feed_version_date, f.gtfs_shape_id_i, f.gtfs_shape_id_v
    FROM ml.trip_validity_final f
    JOIN ml.trip_validity_trips t USING (trip_id)
    WHERE f.is_valid AND f.trip_date = %(date)s;
"""

_POSITIONS_SQL = """
    SELECT
        device_id,
        extract(epoch FROM metric_timestamp)::float8 AS epoch,
        ST_X(ST_Transform(geom, 31984)) AS x,
        ST_Y(ST_Transform(geom, 31984)) AS y,
        speed,
        route_id,
        heading_degrees
    FROM ml.bus_matching_avl_positions
    WHERE metric_timestamp >= %(start)s AND metric_timestamp < %(end)s
      AND device_id = ANY(%(devices)s)
    ORDER BY device_id, epoch;
"""

_FARES_SQL = """
    SELECT ff.trip_id, extract(epoch FROM ff.fare_tapped_at)::float8 AS epoch
    FROM ml.trip_validity_fares_final ff
    JOIN ml.trip_validity_final f USING (trip_id)
    WHERE f.is_valid AND f.trip_date = %(date)s;
"""

_CANDIDATES_SQL = """
    SELECT bus_id, device_id FROM ml.bus_matching_candidates WHERE date = %(date)s;
"""


def _positions_by_device(rows: pd.DataFrame) -> dict[str, TripPositions]:
    """Split one date's bulk position pull into a per-device dict."""
    out: dict[str, TripPositions] = {}
    for device_id, g in rows.groupby("device_id", sort=False):
        out[device_id] = TripPositions(
            epoch=g["epoch"].to_numpy(dtype=np.float64),
            xy=np.column_stack(
                [g["x"].to_numpy(dtype=np.float64), g["y"].to_numpy(dtype=np.float64)]
            ),
            speed_kmh=g["speed"].to_numpy(dtype=np.float64),
            route_id=g["route_id"].to_numpy(dtype=object),
            heading_deg=g["heading_degrees"].to_numpy(dtype=np.float64),
        )
    return out


def build_one_date(trip_date: datetime.date) -> tuple[datetime.date, int, float]:
    """Compute and write every candidate pair's features for one date.

    Args:
        trip_date: The date to build.

    Returns:
        `(trip_date, n_rows_written, elapsed_seconds)`.

    """
    started = time.monotonic()
    with psycopg.connect(settings.db_dsn) as conn:
        shape_cache = build_shape_cache(conn)
        no_avl_buses = {r[0] for r in conn.execute(_NO_AVL_COMPANIES_SQL).fetchall()}

        candidates = pd.read_sql(_CANDIDATES_SQL, conn, params={"date": trip_date})
        candidates = candidates[~candidates["bus_id"].isin(no_avl_buses)]
        if candidates.empty:
            return trip_date, 0, time.monotonic() - started

        trips = pd.read_sql(_TRIPS_SQL, conn, params={"date": trip_date})
        trips = trips[~trips["bus_id"].isin(no_avl_buses)]

        fares = pd.read_sql(_FARES_SQL, conn, params={"date": trip_date})
        positions = pd.read_sql(
            _POSITIONS_SQL,
            conn,
            params={
                "start": trip_date,
                "end": trip_date + datetime.timedelta(days=1),
                "devices": candidates["device_id"].unique().tolist(),
            },
        )

    fares_by_trip = {
        trip_id: g["epoch"].to_numpy(dtype=np.float64)
        for trip_id, g in fares.groupby("trip_id", sort=False)
    }
    device_positions = _positions_by_device(positions)
    trips_by_bus = dict(list(trips.groupby("bus_id", sort=False)))
    sampled_by_bus = {
        bus_id: select_sample_trips(g) for bus_id, g in trips_by_bus.items()
    }

    rows = []
    for bus_id, g in candidates.groupby("bus_id", sort=False):
        all_trips = trips_by_bus.get(bus_id)
        sampled = sampled_by_bus.get(bus_id)
        if sampled is None or sampled.empty:
            continue
        for device_id in g["device_id"]:
            feats = compute_pair_day_features(
                sampled,
                device_positions.get(device_id),
                shape_cache,
                fares_by_trip=fares_by_trip,
                all_trips=all_trips,
            )
            feats["bus_id"] = bus_id
            feats["device_id"] = device_id
            feats["date"] = trip_date
            rows.append(feats)

    frame = pd.DataFrame(
        rows, columns=[*DAY_FEATURE_NAMES, "bus_id", "device_id", "date"]
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(OUTPUT_DIR / f"date={trip_date}.parquet", index=False)
    return trip_date, len(frame), time.monotonic() - started


def all_trip_dates() -> list[datetime.date]:
    """Every date with at least one valid trip, ascending."""
    with psycopg.connect(settings.db_dsn) as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT trip_date FROM ml.trip_validity_final "
                "WHERE is_valid ORDER BY trip_date;"
            ).fetchall()
        ]


def main() -> None:
    """Build features for the requested dates, one process per date."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", nargs="*", help="ISO dates; default all")
    parser.add_argument(
        "--workers",
        type=int,
        default=min(8, (os.cpu_count() or 2)),
        help="parallel worker processes (default: min(8, cpu_count))",
    )
    args = parser.parse_args()

    dates = (
        [datetime.date.fromisoformat(d) for d in args.dates]
        if args.dates
        else all_trip_dates()
    )
    print(f"building {len(dates)} dates with {args.workers} workers -> {OUTPUT_DIR}")

    started = time.monotonic()
    total_rows = 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(build_one_date, d): d for d in dates}
        for done, future in enumerate(as_completed(futures), start=1):
            trip_date, n_rows, elapsed = future.result()
            total_rows += n_rows
            print(
                f"[{done}/{len(dates)}] {trip_date}: {n_rows} rows in {elapsed:.0f}s "
                f"({time.monotonic() - started:.0f}s total)"
            )
    print(f"done: {total_rows} rows in {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    main()
