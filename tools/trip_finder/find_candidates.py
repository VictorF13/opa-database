"""Build the (bad trip x candidate vehicle_id) feature dataset for trip_finder.

For each of November 2023's ~44,347 "not good" AFC trips (no confident
prediction, either valid or invalid, in scratch.trip_match_predictions), pulls
every GPS-tracked vehicle_id active during that exact trip's [trip_opened_at,
trip_closed_at] window and scores it against both directions (-I/-V) of the
trip's own line, using the same distance/line-fraction/progress-correlation/
start-end-proximity formulas as notebooks/_scoring_lib.py's PostGIS query --
just computed locally with shapely instead of a per-row Postgres round trip.

Candidates already confidently matched to a DIFFERENT trip that overlaps this
trip's own window are excluded (time-scoped, not a blanket per-vehicle
exclusion -- a vehicle can be a valid candidate for one trip and correctly
excluded for another later the same day).

Only two Postgres round trips touch anything resembling "the full dataset"
(the bulk pings COPY and the bulk shape/reference loads); every join,
window-slice, and aggregation from there on is local (numpy + polars), per
the explicit constraint that this must run on-machine, not as heavy SQL.

Writes one parquet file per BATCH_SIZE-trip batch under
tools/trip_finder/data/batches/, e.g. batch_0000.parquet for trips 0-99,
batch_0001.parquet for 100-199, etc. -- app.py reads the whole directory as
its candidate pool, so the labeling UI can start as soon as the first batch
exists instead of waiting for all ~44,347 trips to finish. Rerunning this
script skips any batch whose file already exists, so it's safe to leave
running in the background (or restart) while batches accumulate. Does not
touch any gold table, and does not write anything to Postgres.

Run with: uv run tools/trip_finder/find_candidates.py
"""

from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

import numpy as np
import polars as pl
import psycopg
import pyproj
import shapely
from scoring import (
    MIN_PINGS_FOR_CORR,
    UTM_24S,
    ShapeGeom,
    compute_direction_metrics,
    compute_movement_metrics,
    iv_overlap_m,
    load_shapes,
    local_fortaleza,
    project_pings,
    shape_start_end_dist_m,
)

DSN = "postgresql://opa:opa@localhost:5432/opa"
DATE_START = dt.date(2023, 11, 1)
DATE_END = dt.date(2023, 12, 1)

DATA_DIR = Path(__file__).parent / "data"
PINGS_CACHE = DATA_DIR / "avl_pings_2023_11.parquet"
BATCHES_DIR = DATA_DIR / "batches"

# Set TRIP_LIMIT=200 for a fast dev run; unset/0 for the full 44,347 trips.
TRIP_LIMIT = int(os.environ.get("TRIP_LIMIT", "0")) or None
# One parquet file this many trips at a time -- small enough that the first
# batch (and therefore a usable labeling pool) lands within a minute or two,
# not after the full ~44,347-trip run finishes.
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "100"))
# Cap how many NEW batches this invocation processes before exiting; unset/0
# means "keep going until every batch is done" -- the mode used to leave
# this running in the background while the labeling UI works off whatever's
# landed so far.
MAX_BATCHES = int(os.environ.get("MAX_BATCHES", "0")) or None

SPOT_CHECK_SAMPLE = 30
PROGRESS_EVERY = 1000

_WGS84_TO_UTM = pyproj.Transformer.from_crs(4326, UTM_24S, always_xy=True)


def fetch_bad_trips(conn: psycopg.Connection) -> pl.DataFrame:
    """Every Nov-2023 AFC trip with no confident prediction, either way.

    "Confident" means trust_tier in ('high_confidence_valid',
    'high_confidence_invalid') on ANY source's (device/vehicle) prediction
    row for that trip -- a trip resolved as confidently-not-a-match is just
    as "good" (already answered) as a confident match; only the leftover
    unconfident/unscored trips are the ones this dataset is for.

    resolved_feed_version_date falls back to the nearest-by-date feed that
    actually has shapes for that line_number when scratch.trip_feed_resolution
    has no row for (line_number, trip_date) (~6832/44347 trips) -- GTFS
    shapes change rarely enough that "nearest available feed" is a reasonable
    stand-in for "the feed that was actually in service that day, but wasn't
    resolved for some other reason." Trips whose line_number has no shape in
    ANY feed are dropped (can't be scored against a route at all) and counted.
    """
    rows = conn.execute(
        """
        WITH trips AS (
            SELECT DISTINCT vehicle_number, line_number, trip_opened_at, trip_closed_at
            FROM silver.afc_boardings
            WHERE trip_opened_at >= %(start)s AND trip_opened_at < %(end)s
        ),
        matched AS (
            SELECT t.vehicle_number, t.line_number, t.trip_opened_at, t.trip_closed_at,
                   bool_or(
                       p.trust_tier
                       IN ('high_confidence_valid', 'high_confidence_invalid')
                   ) AS has_good
            FROM trips t
            LEFT JOIN scratch.trip_match_predictions p
              ON p.vehicle_number = t.vehicle_number
             AND p.line_number = t.line_number
             AND p.trip_opened_at = t.trip_opened_at
             AND p.trip_closed_at = t.trip_closed_at
            GROUP BY t.vehicle_number, t.line_number, t.trip_opened_at, t.trip_closed_at
        ),
        bad_trips AS (
            SELECT * FROM matched WHERE has_good IS NOT TRUE
        )
        SELECT bt.vehicle_number, bt.line_number, bt.trip_opened_at, bt.trip_closed_at,
               COALESCE(r.resolved_feed_version_date, fb.fallback_feed)
                   AS resolved_feed_version_date
        FROM bad_trips bt
        LEFT JOIN scratch.trip_feed_resolution r
          ON r.line_number = bt.line_number AND r.trip_date = bt.trip_opened_at::date
        LEFT JOIN LATERAL (
            SELECT g.feed_version_date AS fallback_feed
            FROM scratch.route_shape_geoms g
            WHERE g.line_number = bt.line_number
              AND r.resolved_feed_version_date IS NULL
            ORDER BY abs(g.feed_version_date - bt.trip_opened_at::date)
            LIMIT 1
        ) fb ON r.resolved_feed_version_date IS NULL
        ORDER BY bt.line_number, bt.trip_opened_at, bt.vehicle_number
        """,
        {"start": DATE_START, "end": DATE_END},
    ).fetchall()
    df = pl.DataFrame(
        rows,
        schema={
            "vehicle_number": pl.Utf8,
            "line_number": pl.Utf8,
            "trip_opened_at": pl.Datetime("us", "UTC"),
            "trip_closed_at": pl.Datetime("us", "UTC"),
            "resolved_feed_version_date": pl.Date,
        },
        orient="row",
    )
    n_unresolvable = df["resolved_feed_version_date"].is_null().sum()
    if n_unresolvable:
        print(f"  dropping {n_unresolvable} trips whose line has no shape in any feed")
        df = df.filter(pl.col("resolved_feed_version_date").is_not_null())
    return df.with_row_index("trip_id")


def fetch_speed_ref(
    conn: psycopg.Connection, trips: pl.DataFrame
) -> dict[tuple[str, dt.datetime, str], tuple[float, float]]:
    """Precomputed (implied_speed_kmh, speed_percentile) per (trip, shape_id).

    Sourced from scratch.trip_shape_samples_scored, which already encodes the
    historical-percentile reference distribution built in
    notebooks/01b_build_reference_data.py -- reused here instead of
    recomputing that distribution from scratch. Trips resolved through the
    nearest-feed fallback above mostly won't have a row here (that table was
    built from the *original* feed resolution); those get implied_speed_kmh
    computed directly from shape_length_m/duration and speed_percentile left
    NaN, same "let the model see it's missing" convention as the rest of
    this pipeline.
    """
    vehicle_numbers = trips["vehicle_number"].to_list()
    trip_opens = trips["trip_opened_at"].to_list()
    rows = conn.execute(
        """
        SELECT s.vehicle_number, s.trip_opened_at, s.shape_id,
               s.implied_speed_kmh, s.speed_percentile
        FROM scratch.trip_shape_samples_scored s
        JOIN unnest(%(vns)s::text[], %(tos)s::timestamptz[]) AS want(vn, to_)
          ON s.vehicle_number = want.vn AND s.trip_opened_at = want.to_
        """,
        {"vns": vehicle_numbers, "tos": trip_opens},
    ).fetchall()
    return {(r[0], r[1], r[2]): (r[3], r[4]) for r in rows}


def fetch_success_intervals(
    conn: psycopg.Connection,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Time-scoped exclusion set: (vehicle_id, start_epoch, end_epoch) arrays.

    A candidate vehicle_id is excluded from a trip only if one of these
    intervals overlaps that trip's own window -- not a blanket per-vehicle
    exclusion for the whole month.
    """
    rows = conn.execute(
        """
        SELECT entity_id::integer AS vehicle_id,
               extract(epoch FROM trip_opened_at) AS start_epoch,
               extract(epoch FROM trip_closed_at) AS end_epoch
        FROM scratch.trip_match_predictions
        WHERE source = 'vehicle' AND trust_tier = 'high_confidence_valid'
        """
    ).fetchall()
    arr = np.array(rows, dtype=np.float64)
    return arr[:, 0].astype(np.int64), arr[:, 1], arr[:, 2]


def fetch_pings() -> pl.DataFrame:
    """Every AVL ping in [DATE_START, DATE_END), UTM-projected, cached to parquet.

    A COPY ... TO STDOUT CSV round trip (not pd.read_sql/psycopg fetchall) --
    at ~131M rows that's the only way this finishes in a reasonable time.
    Cached locally since this is the expensive step and find_candidates.py
    gets rerun during development.
    """
    if PINGS_CACHE.exists():
        print(f"  loading cached pings from {PINGS_CACHE}")
        return pl.read_parquet(PINGS_CACHE)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_csv = PINGS_CACHE.with_suffix(".csv")
    conn = psycopg.connect(DSN)
    t0 = time.time()
    with (
        conn.cursor() as cur,
        tmp_csv.open("wb") as f,
        cur.copy(
            "COPY (SELECT vehicle_id, metric_timestamp, longitude, latitude "
            "FROM silver.avl_pings "
            "WHERE metric_timestamp >= %(start)s AND metric_timestamp < %(end)s) "
            "TO STDOUT WITH (FORMAT csv)",
            {"start": DATE_START, "end": DATE_END},
        ) as copy,
    ):
        for chunk in copy:
            f.write(bytes(chunk))
    conn.close()
    print(f"  COPY done in {time.time() - t0:.1f}s")

    df = pl.read_csv(
        tmp_csv,
        has_header=False,
        new_columns=["vehicle_id", "metric_timestamp", "longitude", "latitude"],
        schema_overrides={
            "vehicle_id": pl.Int32,
            "longitude": pl.Float64,
            "latitude": pl.Float64,
        },
        try_parse_dates=True,
    )
    tmp_csv.unlink()

    x, y = project_pings(
        df["longitude"].to_numpy(), df["latitude"].to_numpy(), _WGS84_TO_UTM
    )
    df = df.with_columns(
        pl.Series("x", x),
        pl.Series("y", y),
        (pl.col("metric_timestamp").dt.epoch(time_unit="ms") / 1000.0).alias("epoch"),
    ).sort("epoch")
    df.write_parquet(PINGS_CACHE)
    print(f"  {len(df)} pings cached to {PINGS_CACHE}")
    return df


def aggregate_trip_candidates(
    vehicle_id: np.ndarray,
    epoch: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    shape_i: ShapeGeom | None,
    shape_v: ShapeGeom | None,
) -> dict[str, np.ndarray]:
    """Vectorized per-candidate aggregation for one trip's ping slice.

    Fast reimplementation of what scoring.compute_direction_metrics does one
    candidate at a time -- grouped by vehicle_id via np.unique/np.bincount
    instead of a Python-level loop per candidate, since a single trip window
    can have hundreds to ~1500 distinct active vehicles. find_candidates.py's
    spot-check compares a random sample of this function's output against
    compute_direction_metrics called per-candidate, to make sure the two
    never silently disagree.
    """
    order = np.argsort(epoch, kind="mergesort")
    vehicle_id, epoch, x, y = vehicle_id[order], epoch[order], x[order], y[order]
    pts = shapely.points(x, y)
    # Correlation is shift-invariant, so centering here doesn't change the
    # result -- it just keeps the E[t^2]-E[t]^2 moment formula below from
    # squaring raw Unix-epoch values (~1.7e9) and losing all precision to
    # catastrophic cancellation (caught by find_candidates.py's spot_check
    # disagreeing with scoring.compute_direction_metrics's np.corrcoef path).
    epoch = epoch - epoch[0]

    uniq_vehicles, group_idx = np.unique(vehicle_id, return_inverse=True)
    n_groups = len(uniq_vehicles)
    counts = np.bincount(group_idx, minlength=n_groups)

    first_idx = np.unique(group_idx, return_index=True)[1]
    rev = group_idx[::-1]
    last_idx = len(group_idx) - 1 - np.unique(rev, return_index=True)[1]

    out: dict[str, np.ndarray] = {
        "vehicle_id": uniq_vehicles,
        "n_pings_in_window": counts,
    }

    for suffix, shape in (("ida", shape_i), ("volta", shape_v)):
        if shape is None:
            nan = np.full(n_groups, np.nan)
            out[f"{suffix}_avg_dist_to_line_m"] = nan
            out[f"{suffix}_progress_corr"] = nan
            out[f"{suffix}_start_proximity_m"] = nan
            out[f"{suffix}_end_proximity_m"] = nan
            continue

        dist = shapely.distance(pts, shape.line)
        frac = shapely.line_locate_point(shape.line, pts, normalized=True)
        dist_start = shapely.distance(pts, shape.start_point)
        dist_end = shapely.distance(pts, shape.end_point)

        sums = np.bincount(group_idx, weights=dist, minlength=n_groups)
        out[f"{suffix}_avg_dist_to_line_m"] = sums / counts

        n = counts.astype(np.float64)
        st = np.bincount(group_idx, weights=epoch, minlength=n_groups)
        sf = np.bincount(group_idx, weights=frac, minlength=n_groups)
        stf = np.bincount(group_idx, weights=epoch * frac, minlength=n_groups)
        st2 = np.bincount(group_idx, weights=epoch**2, minlength=n_groups)
        sf2 = np.bincount(group_idx, weights=frac**2, minlength=n_groups)
        with np.errstate(invalid="ignore", divide="ignore"):
            cov = stf / n - (st / n) * (sf / n)
            var_t = st2 / n - (st / n) ** 2
            var_f = sf2 / n - (sf / n) ** 2
            corr = cov / np.sqrt(var_t * var_f)
        corr = np.where(
            (counts >= MIN_PINGS_FOR_CORR) & (var_t > 0) & (var_f > 0), corr, np.nan
        )
        out[f"{suffix}_progress_corr"] = corr

        out[f"{suffix}_start_proximity_m"] = dist_start[first_idx]
        out[f"{suffix}_end_proximity_m"] = dist_end[last_idx]

    out.update(
        _aggregate_movement_metrics(
            x, y, epoch, group_idx, n_groups, counts, first_idx, last_idx
        )
    )
    return out


def _aggregate_movement_metrics(
    x: np.ndarray,
    y: np.ndarray,
    epoch: np.ndarray,
    group_idx: np.ndarray,
    n_groups: int,
    counts: np.ndarray,
    first_idx: np.ndarray,
    last_idx: np.ndarray,
) -> dict[str, np.ndarray]:
    """Vectorized per-group total_distance_m/ping_timespan_sec/spatial_dispersion_m.

    The candidate's own trace, independent of any shape -- see
    scoring.compute_movement_metrics for the canonical per-candidate
    reference this mirrors. Split out of aggregate_trip_candidates purely to
    keep that function under the statement-count lint limit.

    total_distance_m needs consecutive-in-time pings of the SAME group,
    which aren't adjacent rows in the (globally time-sorted) input arrays --
    other vehicles' pings interleave between them. Re-sorting by (group,
    time) makes each group's own pings contiguous and in order, so a plain
    np.diff gives consecutive-step distances directly; the first row of each
    new group has to be masked out since it isn't actually consecutive with
    the previous group's last row.
    """
    lex_order = np.lexsort((epoch, group_idx))
    gx, gy, ggroup = x[lex_order], y[lex_order], group_idx[lex_order]
    step_dist = np.hypot(np.diff(gx), np.diff(gy))
    same_group = ggroup[1:] == ggroup[:-1]
    step_dist = np.where(same_group, step_dist, 0.0)
    total_distance = np.bincount(ggroup[1:], weights=step_dist, minlength=n_groups)

    # radius of gyration: RMS distance from the group's own centroid -- an
    # O(n) proxy for "how spread out are these points" (same idea as mean
    # pairwise distance, without the O(n^2) cost at up to ~1500 pings/group).
    mean_x = np.bincount(group_idx, weights=x, minlength=n_groups) / counts
    mean_y = np.bincount(group_idx, weights=y, minlength=n_groups) / counts
    sq_dist = (x - mean_x[group_idx]) ** 2 + (y - mean_y[group_idx]) ** 2
    sum_sq = np.bincount(group_idx, weights=sq_dist, minlength=n_groups)

    enough = counts >= MIN_PINGS_FOR_CORR
    return {
        "total_distance_m": np.where(enough, total_distance, np.nan),
        "ping_timespan_sec": np.where(
            enough, epoch[last_idx] - epoch[first_idx], np.nan
        ),
        "spatial_dispersion_m": np.sqrt(sum_sq / counts),
    }


def spot_check(
    trips: pl.DataFrame,
    pings: pl.DataFrame,
    shapes: dict[tuple[dt.date, str, str], ShapeGeom],
    result: pl.DataFrame,
    rng: np.random.Generator,
) -> None:
    """Recompute a random sample of output rows via the per-candidate scalar path.

    Compares aggregate_trip_candidates's batched output against
    scoring.compute_direction_metrics run on that exact (trip, candidate)
    pair's own raw pings -- if these ever disagree, the batched aggregation
    has a bug and nothing downstream can be trusted.
    """
    sample_size = min(SPOT_CHECK_SAMPLE, len(result))
    idx = rng.choice(len(result), size=sample_size, replace=False)
    sample = result[idx.tolist()]
    trips_by_id = {row["trip_id"]: row for row in trips.iter_rows(named=True)}
    mismatches = 0
    for row in sample.iter_rows(named=True):
        trip = trips_by_id[row["trip_id"]]
        window = pings.filter(
            (pl.col("vehicle_id") == row["candidate_vehicle_id"])
            & (pl.col("metric_timestamp") >= trip["trip_opened_at"])
            & (pl.col("metric_timestamp") <= trip["trip_closed_at"])
        )
        px, py, pe = (
            window["x"].to_numpy(),
            window["y"].to_numpy(),
            window["epoch"].to_numpy(),
        )
        for suffix, direction in (("ida", "I"), ("volta", "V")):
            shape = shapes.get(
                (trip["resolved_feed_version_date"], trip["line_number"], direction)
            )
            expected = compute_direction_metrics(px, py, pe, shape)
            checks = [
                (expected.n_pings_in_window, row["n_pings_in_window"], 1e-6, False),
                (
                    expected.avg_dist_to_line_m,
                    row[f"{suffix}_avg_dist_to_line_m"],
                    1e-6,
                    False,
                ),
                (expected.progress_corr, row[f"{suffix}_progress_corr"], 1e-4, True),
                (
                    expected.start_proximity_m,
                    row[f"{suffix}_start_proximity_m"],
                    1e-6,
                    False,
                ),
                (
                    expected.end_proximity_m,
                    row[f"{suffix}_end_proximity_m"],
                    1e-6,
                    False,
                ),
            ]
            for exp, act, atol, nan_near_zero_ok in checks:
                # progress_corr near a zero-variance (stationary-along-line)
                # boundary: one implementation's variance rounds to exactly
                # zero (NaN) while the other has residual float noise a hair
                # above zero -- both mean "no discernible progress," so treat
                # NaN vs. a near-zero value as agreeing rather than a mismatch.
                if (
                    nan_near_zero_ok
                    and (np.isnan(exp) or np.isnan(act))
                    and not (np.isnan(exp) and np.isnan(act))
                ):
                    other = act if np.isnan(exp) else exp
                    if abs(other) < atol:
                        continue
                if not np.isclose(exp, act, equal_nan=True, rtol=atol, atol=atol):
                    mismatches += 1
                    print(
                        f"  MISMATCH trip={row['trip_id']} "
                        f"vehicle={row['candidate_vehicle_id']} {suffix}: "
                        f"expected {exp}, got {act}"
                    )

        move_expected = compute_movement_metrics(px, py, pe)
        move_checks = [
            (move_expected.total_distance_m, row["total_distance_m"]),
            (move_expected.ping_timespan_sec, row["ping_timespan_sec"]),
            (move_expected.spatial_dispersion_m, row["spatial_dispersion_m"]),
        ]
        for exp, act in move_checks:
            if not np.isclose(exp, act, equal_nan=True, rtol=1e-4, atol=1e-4):
                mismatches += 1
                print(
                    f"  MISMATCH trip={row['trip_id']} "
                    f"vehicle={row['candidate_vehicle_id']} movement: "
                    f"expected {exp}, got {act}"
                )
    if mismatches:
        msg = (
            f"{mismatches} spot-checked values disagree with "
            "scoring.compute_direction_metrics"
        )
        raise RuntimeError(msg)
    print(f"  spot check passed: {len(sample)}/{len(sample)} rows match")


def _lookup_speed(
    speed_ref: dict[tuple[str, dt.datetime, str], tuple[float, float]],
    vehicle_number: str,
    t_open: dt.datetime,
    shape: ShapeGeom | None,
    duration_sec: float,
) -> tuple[float, float]:
    """Look up (implied_speed_kmh, speed_percentile) for one (trip, direction).

    Falls back to a directly-computed implied speed (shape length / trip
    duration) with NaN percentile when the trip has no precomputed row in
    scratch.trip_shape_samples_scored (mostly the ~6832 trips resolved
    through the nearest-feed fallback, see fetch_bad_trips).
    """
    speed, pct = speed_ref.get(
        (vehicle_number, t_open, shape.shape_id if shape else ""), (np.nan, np.nan)
    )
    if np.isnan(speed) and shape is not None and duration_sec > 0:
        speed = shape.length_m / 1000 / (duration_sec / 3600)
    return speed, pct


def score_one_trip(
    trip: dict,
    ping_vehicle: np.ndarray,
    ping_epoch: np.ndarray,
    ping_x: np.ndarray,
    ping_y: np.ndarray,
    shapes: dict[tuple[dt.date, str, str], ShapeGeom],
    overlap: dict[tuple[dt.date, str], float],
    start_end_dist: dict[tuple[dt.date, str, str], float],
    speed_ref: dict[tuple[str, dt.datetime, str], tuple[float, float]],
    succ_vehicle: np.ndarray,
    succ_start: np.ndarray,
    succ_end: np.ndarray,
) -> pl.DataFrame | None:
    """Score every active candidate vehicle_id for one trip, or None if none qualify."""
    t_open, t_close = trip["trip_opened_at"], trip["trip_closed_at"]
    lo = np.searchsorted(ping_epoch, t_open.timestamp(), side="left")
    hi = np.searchsorted(ping_epoch, t_close.timestamp(), side="right")
    if hi <= lo:
        return None

    feed, line = trip["resolved_feed_version_date"], trip["line_number"]
    shape_i, shape_v = shapes.get((feed, line, "I")), shapes.get((feed, line, "V"))
    agg = aggregate_trip_candidates(
        ping_vehicle[lo:hi],
        ping_epoch[lo:hi],
        ping_x[lo:hi],
        ping_y[lo:hi],
        shape_i,
        shape_v,
    )

    excluded = (succ_start < t_close.timestamp()) & (succ_end > t_open.timestamp())
    excluded_vehicles = set(succ_vehicle[excluded].tolist())
    keep = np.array([v not in excluded_vehicles for v in agg["vehicle_id"]], dtype=bool)
    if not keep.any():
        return None

    local_open, local_close = local_fortaleza(t_open), local_fortaleza(t_close)
    duration_sec = (t_close - t_open).total_seconds()
    ida_speed, ida_pct = _lookup_speed(
        speed_ref, trip["vehicle_number"], t_open, shape_i, duration_sec
    )
    volta_speed, volta_pct = _lookup_speed(
        speed_ref, trip["vehicle_number"], t_open, shape_v, duration_sec
    )

    batch = pl.DataFrame({k: v[keep] for k, v in agg.items()}).rename(
        {"vehicle_id": "candidate_vehicle_id"}
    )
    return batch.with_columns(
        trip_id=pl.lit(trip["trip_id"]),
        vehicle_number=pl.lit(trip["vehicle_number"]),
        line_number=pl.lit(line),
        trip_opened_at=pl.lit(t_open),
        trip_closed_at=pl.lit(t_close),
        resolved_feed_version_date=pl.lit(feed),
        duration_sec=pl.lit(duration_sec),
        day_of_week=pl.lit(local_open.weekday(), dtype=pl.Int8),
        hour_of_trip_start=pl.lit(local_open.hour, dtype=pl.Int8),
        hour_of_trip_end=pl.lit(local_close.hour, dtype=pl.Int8),
        iv_overlap_m=pl.lit(overlap.get((feed, line), np.nan)),
        ida_shape_start_end_dist_m=pl.lit(
            start_end_dist.get((feed, line, "I"), np.nan)
        ),
        volta_shape_start_end_dist_m=pl.lit(
            start_end_dist.get((feed, line, "V"), np.nan)
        ),
        ida_implied_speed_kmh=pl.lit(ida_speed),
        ida_speed_percentile=pl.lit(ida_pct),
        volta_implied_speed_kmh=pl.lit(volta_speed),
        volta_speed_percentile=pl.lit(volta_pct),
    )


def load_reference_data(conn: psycopg.Connection, trips: pl.DataFrame) -> dict:
    """Load every static/reference input score_one_trip needs, once, up front."""
    print("loading route shapes")
    pairs = trips.select("resolved_feed_version_date", "line_number").unique().rows()
    shapes = load_shapes(conn, pairs)
    print(f"  {len(shapes)} shapes loaded for {len(pairs)} (feed, line) pairs")

    print("loading speed reference")
    speed_ref = fetch_speed_ref(conn, trips)
    print(f"  {len(speed_ref)} (trip, shape) speed rows")

    print("loading success-group intervals")
    succ_vehicle, succ_start, succ_end = fetch_success_intervals(conn)
    print(f"  {len(succ_vehicle)} intervals")

    return {
        "shapes": shapes,
        "overlap": iv_overlap_m(shapes),
        "start_end_dist": shape_start_end_dist_m(shapes),
        "speed_ref": speed_ref,
        "succ_vehicle": succ_vehicle,
        "succ_start": succ_start,
        "succ_end": succ_end,
    }


def score_trips(
    trips: pl.DataFrame, pings: pl.DataFrame, ref: dict
) -> tuple[pl.DataFrame | None, int]:
    """Sequentially score every trip in `trips` (one batch's worth).

    Returns (result_or_None, n_empty); result is None if every trip in this
    batch had zero surviving candidates (rare, but not impossible for a
    lightly-covered window).
    """
    ping_vehicle = pings["vehicle_id"].to_numpy()
    ping_epoch = pings["epoch"].to_numpy()
    ping_x = pings["x"].to_numpy()
    ping_y = pings["y"].to_numpy()

    batches: list[pl.DataFrame] = []
    n_no_candidates = 0
    for trip in trips.iter_rows(named=True):
        batch = score_one_trip(
            trip,
            ping_vehicle,
            ping_epoch,
            ping_x,
            ping_y,
            ref["shapes"],
            ref["overlap"],
            ref["start_end_dist"],
            ref["speed_ref"],
            ref["succ_vehicle"],
            ref["succ_start"],
            ref["succ_end"],
        )
        if batch is None:
            n_no_candidates += 1
        else:
            batches.append(batch)

    return (pl.concat(batches) if batches else None), n_no_candidates


def _batch_path(batch_idx: int) -> Path:
    return BATCHES_DIR / f"batch_{batch_idx:04d}.parquet"


def main() -> None:
    """Score trips in resumable BATCH_SIZE-trip chunks, one parquet file each.

    Skips any batch whose output file already exists, so this can be killed
    and rerun (or left running for hours) without redoing finished work --
    app.py can start reading tools/trip_finder/data/batches/ as soon as
    batch_0000.parquet exists, well before this finishes the full trip list.
    """
    BATCHES_DIR.mkdir(parents=True, exist_ok=True)
    conn = psycopg.connect(DSN)

    print("fetching bad trips")
    trips = fetch_bad_trips(conn)
    if TRIP_LIMIT:
        trips = trips.head(TRIP_LIMIT)
    n_batches = -(-len(trips) // BATCH_SIZE)  # ceil division
    print(f"  {len(trips)} trips, {n_batches} batches of {BATCH_SIZE}")

    print("loading reference data (shapes, speed, success intervals)")
    ref = load_reference_data(conn, trips)

    print("fetching Nov 2023 fleet pings")
    pings = fetch_pings()
    print(f"  {len(pings)} pings")
    conn.close()

    rng = np.random.default_rng(seed=0)
    n_done_this_run = 0
    t0 = time.time()
    for batch_idx in range(n_batches):
        out_path = _batch_path(batch_idx)
        if out_path.exists():
            continue
        if MAX_BATCHES and n_done_this_run >= MAX_BATCHES:
            print(f"  reached MAX_BATCHES={MAX_BATCHES}, stopping")
            break

        batch_trips = trips.slice(batch_idx * BATCH_SIZE, BATCH_SIZE)
        result, n_empty = score_trips(batch_trips, pings, ref)
        if result is None:
            print(f"  batch {batch_idx}: 0 candidate rows (all {n_empty} trips empty)")
            continue

        spot_check(batch_trips, pings, ref["shapes"], result, rng)
        result.write_parquet(out_path)
        n_done_this_run += 1

        elapsed = time.time() - t0
        rate = n_done_this_run / elapsed
        remaining = n_batches - batch_idx - 1
        eta_min = remaining / rate / 60 if rate > 0 else float("nan")
        print(
            f"  batch {batch_idx}/{n_batches}: {len(result)} rows "
            f"({n_empty} trips empty), {rate:.2f} batches/s, eta {eta_min:.1f} min"
        )

    print("done" if n_done_this_run else "nothing to do, all batches already exist")


if __name__ == "__main__":
    main()
