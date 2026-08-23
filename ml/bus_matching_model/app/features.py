"""Tier 1 feature computation for the Bus Matching model.

Computes, per `(bus_id, device_id, date, trip)`, linear-referencing and
kinematic features from one date's AVL positions projected onto the
`gtfs_cache` shape arrays, then aggregates to one row per
`(bus_id, device_id, date)` -- the model's actual prediction unit.
Everything here is pure numpy/pandas against one bulk per-date pull;
there is no per-pair database query.

Tier 2 (fare timing, day-level continuity, Frechet/Hausdorff) is
deliberately not implemented yet -- Tier 1 alone is enough to look at
score separation and start labeling; Tier 2 is only worth the extra
cost for the top few candidates per bus-date once Tier 1 narrows the
field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from gtfs_cache import min_distance_to_each, project_points

if TYPE_CHECKING:
    import datetime

    from gtfs_cache import RouteShape

OFFSET_THRESHOLDS_M = (30.0, 50.0, 100.0)
CONTRADICTION_THRESHOLD_M = 500.0
STOP_COINCIDENCE_M = 60.0
STATIONARY_SPEED_KMH = 3.0
CHAINAGE_BIN_M = 200.0
MAX_TRIPS_PER_BUS_DATE = 8
GOOD_TRIP_BUFFER_50_MIN = 0.5
GOOD_TRIP_DIRECTION_MARGIN_MIN = 0.3

MIN_POINTS_FOR_CORRELATION = 3
MIN_POINTS_FOR_MONOTONICITY = 2
MIN_POINTS_FOR_DISTANCE = 2
VISITED_BIN_OFFSET_M = 100.0
N_GTFS_DIRECTIONS = 2
BAD_TRIP_CONTRADICTION_MIN = 0.5

DAY_FEATURE_NAMES = [
    "n_trips_sampled",
    "n_trips_with_data",
    "n_trips_no_avl",
    "n_trips_no_gtfs",
    "frac_good_trips",
    "n_clearly_bad_trips",
    "median_buffer_coverage_50",
    "median_shape_coverage",
    "median_direction_margin",
    "median_distance_ratio",
    "median_mean_speed",
    "median_stop_coincidence_fraction",
    "median_route_id_agreement",
    "median_offset_m",
    "p90_offset_m",
    "worst_contradiction_fraction",
    "max_excursion_m",
    "median_start_dist_route_endpoint_m",
    "median_end_dist_route_endpoint_m",
    "median_start_dist_stop_m",
    "median_end_dist_stop_m",
    "avl_points_per_minute",
    "longest_gap_seconds",
]


@dataclass
class TripPositions:
    """One device's AVL points for one day, ready for window slicing.

    Attributes:
        epoch: `(n,)` seconds since epoch, sorted ascending.
        xy: `(n, 2)` metric coordinates (SRID 31984).
        speed_kmh: `(n,)` reported speed.
        route_id: `(n,)` AVL-reported route id, as text.

    """

    epoch: np.ndarray
    xy: np.ndarray
    speed_kmh: np.ndarray
    route_id: np.ndarray

    def window(self, start_epoch: float, end_epoch: float) -> TripPositions:
        """Slice to the points inside `[start_epoch, end_epoch]`."""
        lo = np.searchsorted(self.epoch, start_epoch, side="left")
        hi = np.searchsorted(self.epoch, end_epoch, side="right")
        return TripPositions(
            epoch=self.epoch[lo:hi],
            xy=self.xy[lo:hi],
            speed_kmh=self.speed_kmh[lo:hi],
            route_id=self.route_id[lo:hi],
        )


def _nearest_dist(point: np.ndarray, candidates: np.ndarray) -> float | None:
    if candidates.shape[0] == 0:
        return None
    return float(min_distance_to_each(point[None, :], candidates)[0])


def _rank(a: np.ndarray) -> np.ndarray:
    """Fast rank via a double argsort.

    Doesn't average-rank ties (unlike `scipy.stats.rankdata`) -- fine
    here since `timestamp`/`chainage` are continuous float64s where
    exact ties are effectively impossible, and this is the hot path.
    """
    order = a.argsort()
    ranks = np.empty(len(a), dtype=np.float64)
    ranks[order] = np.arange(len(a))
    return ranks


def _fast_spearman(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman correlation without scipy's per-call validation overhead.

    Called once per candidate pair per trip per direction (hundreds of
    thousands of times for a full month), where `scipy.stats.spearmanr`'s
    generality overhead dominates its actual (tiny-array) work.
    """
    rx = _rank(x) - (len(x) - 1) / 2.0
    ry = _rank(y) - (len(y) - 1) / 2.0
    denom = np.sqrt((rx * rx).sum() * (ry * ry).sum())
    if denom == 0.0:
        return 0.0
    return float((rx * ry).sum() / denom)


def compute_trip_direction_features(
    points: TripPositions,
    shape: RouteShape,
    trip_route_id: str,
) -> dict[str, float]:
    """Compute one trip's features against one direction's shape.

    Args:
        points: The device's AVL points inside this trip's time window.
        shape: The candidate direction's `RouteShape` from the cache.
        trip_route_id: The trip's own AFC `route_id`, for the
            route-id-agreement feature.

    Returns:
        A flat dict of per-trip-direction metrics. Callers pick the
        winning direction by `direction_correlation` before aggregating.

    """
    n = points.xy.shape[0]
    chainage, offset = project_points(shape, points.xy)

    corr = 0.0
    has_spread = np.ptp(points.epoch) > 0 and np.ptp(chainage) > 0
    if n >= MIN_POINTS_FOR_CORRELATION and has_spread:
        corr = _fast_spearman(points.epoch, chainage)

    monotonic_frac = (
        float(np.mean(np.diff(chainage) >= 0))
        if n >= MIN_POINTS_FOR_MONOTONICITY
        else 0.0
    )

    buffer_coverage = {
        f"buffer_coverage_{int(t)}": float(np.mean(offset <= t)) if n else 0.0
        for t in OFFSET_THRESHOLDS_M
    }

    n_bins = max(int(np.ceil(shape.total_length / CHAINAGE_BIN_M)), 1)
    visited_bins = (
        set((chainage[offset <= VISITED_BIN_OFFSET_M] // CHAINAGE_BIN_M).astype(int))
        if n
        else set()
    )
    shape_coverage = len(visited_bins) / n_bins

    point_to_point = (
        float(np.sqrt((np.diff(points.xy, axis=0) ** 2).sum(axis=1)).sum())
        if n >= MIN_POINTS_FOR_DISTANCE
        else 0.0
    )
    distance_ratio = (
        point_to_point / shape.total_length if shape.total_length > 0 else 0.0
    )

    stationary = points.speed_kmh <= STATIONARY_SPEED_KMH
    if stationary.any() and shape.stop_points.shape[0] > 0:
        stop_dists = min_distance_to_each(points.xy[stationary], shape.stop_points)
        stop_coincidence = float(np.mean(stop_dists <= STOP_COINCIDENCE_M))
    else:
        stop_coincidence = 0.0

    route_agreement = float(np.mean(points.route_id == trip_route_id)) if n else 0.0

    start_route_dist = (
        _nearest_dist(points.xy[0], shape.start_point[None, :]) if n else None
    )
    end_route_dist = (
        _nearest_dist(points.xy[-1], shape.end_point[None, :]) if n else None
    )
    start_stop_dist = (
        _nearest_dist(points.xy[0], shape.first_stop_points) if n else None
    )
    end_stop_dist = _nearest_dist(points.xy[-1], shape.last_stop_points) if n else None

    return {
        "n_points": n,
        "direction_correlation": corr,
        "monotonicity_fraction": monotonic_frac,
        **buffer_coverage,
        "shape_coverage": shape_coverage,
        "median_offset_m": float(np.median(offset)) if n else np.nan,
        "p90_offset_m": float(np.percentile(offset, 90)) if n else np.nan,
        "distance_ratio": distance_ratio,
        "mean_speed_kmh": float(points.speed_kmh.mean()) if n else np.nan,
        "max_speed_kmh": float(points.speed_kmh.max()) if n else np.nan,
        "stationary_fraction": float(stationary.mean()) if n else 0.0,
        "stop_coincidence_fraction": stop_coincidence,
        "contradiction_fraction": (
            float(np.mean(offset > CONTRADICTION_THRESHOLD_M)) if n else 1.0
        ),
        "max_excursion_m": float(offset.max()) if n else np.nan,
        "route_id_agreement": route_agreement,
        "start_dist_route_endpoint_m": start_route_dist,
        "end_dist_route_endpoint_m": end_route_dist,
        "start_dist_stop_m": start_stop_dist,
        "end_dist_stop_m": end_stop_dist,
    }


def select_sample_trips(bus_trips: pd.DataFrame) -> pd.DataFrame:
    """Pick up to `MAX_TRIPS_PER_BUS_DATE` trips spread across the day.

    Args:
        bus_trips: One bus's valid trips for one date, any order.

    Returns:
        A time-sorted subset, evenly spaced by index when there are more
        than `MAX_TRIPS_PER_BUS_DATE` trips, so the sample spans the
        whole operating day rather than clustering at its start.

    """
    ordered = bus_trips.sort_values("trip_start_timestamp").reset_index(drop=True)
    if len(ordered) <= MAX_TRIPS_PER_BUS_DATE:
        return ordered
    idx = np.linspace(0, len(ordered) - 1, MAX_TRIPS_PER_BUS_DATE).round().astype(int)
    return ordered.iloc[np.unique(idx)].reset_index(drop=True)


def _matched_shapes(
    feed_version_date: datetime.date,
    shape_id_i: str | None,
    shape_id_v: str | None,
    shape_cache: dict[tuple, RouteShape],
) -> list[RouteShape]:
    """GTFS shapes a trip's I/V directions resolve to in `shape_cache`."""
    candidates = []
    for shape_id in (shape_id_i, shape_id_v):
        if shape_id is None or pd.isna(shape_id):
            continue
        key = (feed_version_date, shape_id)
        if key in shape_cache:
            candidates.append(shape_cache[key])
    return candidates


def compute_pair_day_features(
    sampled_trips: pd.DataFrame,
    device_positions: TripPositions | None,
    shape_cache: dict[tuple, RouteShape],
) -> dict[str, float]:
    """Aggregate one candidate `(bus_id, device_id, date)` pair to one row.

    Args:
        sampled_trips: This bus's sampled trips for the date (from
            `select_sample_trips`), columns include
            `trip_start_timestamp`/`trip_end_timestamp` (epoch seconds),
            `route_id`, `gtfs_feed_version_date`, `gtfs_shape_id_i`,
            `gtfs_shape_id_v`.
        device_positions: The candidate device's full day of AVL points,
            or `None` if the device has zero pings that day (dead-AVL
            day -- every trip is skipped, never scored negative).
        shape_cache: The `gtfs_cache.build_shape_cache` result.

    Returns:
        A flat dict with every name in `DAY_FEATURE_NAMES`.

    """
    per_trip: list[dict[str, float]] = []
    n_no_avl = 0
    n_no_gtfs = 0

    for trip in sampled_trips.itertuples(index=False):
        candidates = _matched_shapes(
            trip.gtfs_feed_version_date,
            trip.gtfs_shape_id_i,
            trip.gtfs_shape_id_v,
            shape_cache,
        )
        if not candidates:
            n_no_gtfs += 1
            continue
        if device_positions is None:
            n_no_avl += 1
            continue
        window = device_positions.window(
            trip.trip_start_timestamp, trip.trip_end_timestamp
        )
        if window.xy.shape[0] == 0:
            n_no_avl += 1
            continue

        direction_results = [
            compute_trip_direction_features(window, shape, trip.route_id)
            for shape in candidates
        ]
        best = max(direction_results, key=lambda r: r["direction_correlation"])
        if len(direction_results) == N_GTFS_DIRECTIONS:
            best["direction_margin"] = abs(
                direction_results[0]["direction_correlation"]
                - direction_results[1]["direction_correlation"]
            )
        else:
            best["direction_margin"] = abs(best["direction_correlation"])
        gaps = np.diff(window.epoch)
        best["longest_gap_seconds"] = float(gaps.max()) if gaps.size else 0.0
        duration_min = (trip.trip_end_timestamp - trip.trip_start_timestamp) / 60.0
        best["points_per_minute"] = (
            window.xy.shape[0] / duration_min if duration_min > 0 else 0.0
        )
        per_trip.append(best)

    n_sampled = len(sampled_trips)
    n_with_data = len(per_trip)

    if n_with_data == 0:
        return {
            "n_trips_sampled": n_sampled,
            "n_trips_with_data": 0,
            "n_trips_no_avl": n_no_avl,
            "n_trips_no_gtfs": n_no_gtfs,
            "frac_good_trips": np.nan,
            "n_clearly_bad_trips": 0,
            **dict.fromkeys(DAY_FEATURE_NAMES[6:], np.nan),
        }

    def col(name: str) -> np.ndarray:
        return np.array([row[name] for row in per_trip], dtype=np.float64)

    buffer_coverage_50 = col("buffer_coverage_50")
    direction_margin = col("direction_margin")
    contradiction_fraction = col("contradiction_fraction")
    good = (buffer_coverage_50 >= GOOD_TRIP_BUFFER_50_MIN) & (
        direction_margin >= GOOD_TRIP_DIRECTION_MARGIN_MIN
    )
    bad = contradiction_fraction >= BAD_TRIP_CONTRADICTION_MIN

    return {
        "n_trips_sampled": n_sampled,
        "n_trips_with_data": n_with_data,
        "n_trips_no_avl": n_no_avl,
        "n_trips_no_gtfs": n_no_gtfs,
        "frac_good_trips": float(good.mean()),
        "n_clearly_bad_trips": int(bad.sum()),
        "median_buffer_coverage_50": float(np.median(buffer_coverage_50)),
        "median_shape_coverage": float(np.median(col("shape_coverage"))),
        "median_direction_margin": float(np.median(direction_margin)),
        "median_distance_ratio": float(np.median(col("distance_ratio"))),
        "median_mean_speed": float(np.nanmedian(col("mean_speed_kmh"))),
        "median_stop_coincidence_fraction": float(
            np.median(col("stop_coincidence_fraction"))
        ),
        "median_route_id_agreement": float(np.median(col("route_id_agreement"))),
        "median_offset_m": float(np.nanmedian(col("median_offset_m"))),
        "p90_offset_m": float(np.nanmedian(col("p90_offset_m"))),
        "worst_contradiction_fraction": float(contradiction_fraction.max()),
        "max_excursion_m": float(np.nanmax(col("max_excursion_m"))),
        "median_start_dist_route_endpoint_m": float(
            np.nanmedian(col("start_dist_route_endpoint_m"))
        ),
        "median_end_dist_route_endpoint_m": float(
            np.nanmedian(col("end_dist_route_endpoint_m"))
        ),
        "median_start_dist_stop_m": float(np.nanmedian(col("start_dist_stop_m"))),
        "median_end_dist_stop_m": float(np.nanmedian(col("end_dist_stop_m"))),
        "avl_points_per_minute": float(np.median(col("points_per_minute"))),
        "longest_gap_seconds": float(col("longest_gap_seconds").max()),
    }
