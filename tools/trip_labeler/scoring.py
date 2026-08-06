"""Shared candidate/feature-vector definitions for the trip-match pipeline.

Used by both the live labeling app (app.py) and batch inference scripts
(e.g. predict_trips.py). Kept as its own module specifically so the two can
never silently compute features differently: the models saved under
model_store/ are only meaningful if inference builds the exact same
feature vector a label was trained on.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

    import psycopg

SOURCES: dict[str, dict[str, str]] = {
    "device": {"table": "scratch.device_mapping_trip_scores", "key_col": "device_id"},
    "vehicle": {
        "table": "scratch.vehicle_mapping_trip_scores",
        "key_col": "vehicle_id",
    },
}

TWO_CANDIDATES = 2  # a window has at most two direction candidates (-I and -V)


def nan_if_none(v: float | None) -> float:
    """Convert a possibly-missing raw metric into a float, using NaN for None.

    HistGradientBoostingClassifier treats NaN as its own "missing" branch,
    so this is deliberate, not a placeholder that needs imputing later.
    """
    return math.nan if v is None else float(v)


@dataclass
class Candidate:
    """One scored candidate (a single shape direction) for a trip window."""

    shape_id: str
    line_number: str
    direction_id: int | None
    avg_dist_to_line_m: float | None
    progress_corr: float | None
    start_proximity_m: float | None
    end_proximity_m: float | None
    speed_percentile: float | None
    implied_speed_kmh: float | None
    n_pings_in_window: int | None
    duration_sec: float | None


def best_candidate(candidates: list[Candidate]) -> Candidate:
    """Return the candidate with the smallest avg_dist_to_line_m."""
    return min(
        candidates,
        key=lambda c: (
            c.avg_dist_to_line_m if c.avg_dist_to_line_m is not None else math.inf
        ),
    )


def compute_feature_vector(
    candidates: list[Candidate],
    resolved_feed_version_date: date,
    iv_overlap_m: dict[tuple[date, str], float],
    shape_start_end_dist_m: dict[tuple[date, str, str], float],
) -> list[float]:
    """Build the shared feature vector both models are trained/predicted on.

    Built from the best candidate + gap to runner-up, plus static
    route-geometry features (line-level, not trip-level): `iv_overlap_m` is
    how closely the line's own -I/-V shapes overlap each other (small = the
    two directions run the same street, structurally harder to tell apart
    from GPS alone), and `shape_start_end_dist_m` is the straight-line
    distance between a shape's own start and end (small = loop route, large
    = point-to-point).
    """
    best = best_candidate(candidates)
    rest = [c for c in candidates if c is not best]
    gap = math.nan
    if (
        rest
        and rest[0].avg_dist_to_line_m is not None
        and best.avg_dist_to_line_m is not None
    ):
        gap = rest[0].avg_dist_to_line_m - best.avg_dist_to_line_m
    overlap = iv_overlap_m.get((resolved_feed_version_date, best.line_number), math.nan)
    start_end_dist = shape_start_end_dist_m.get(
        (resolved_feed_version_date, best.line_number, best.shape_id), math.nan
    )
    return [
        nan_if_none(best.avg_dist_to_line_m),
        nan_if_none(best.progress_corr),
        nan_if_none(best.start_proximity_m),
        nan_if_none(best.end_proximity_m),
        nan_if_none(best.speed_percentile),
        nan_if_none(best.implied_speed_kmh),
        nan_if_none(best.n_pings_in_window),
        gap,
        overlap,
        start_end_dist,
    ]


def load_iv_overlap(conn: psycopg.Connection) -> dict[tuple[date, str], float]:
    """How closely each line's own -I/-V shapes overlap each other.

    Once per (feed_version_date, line_number). Static route geometry -
    cheap to load in full each time this is called.
    """
    rows = conn.execute(
        """
        SELECT a.feed_version_date, a.line_number,
               ST_HausdorffDistance(a.line_geom_proj, b.line_geom_proj)
        FROM scratch.route_shape_geoms a
        JOIN scratch.route_shape_geoms b
            ON a.feed_version_date = b.feed_version_date
           AND a.line_number = b.line_number
           AND a.shape_id < b.shape_id
        """
    ).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def load_shape_start_end_dist(
    conn: psycopg.Connection,
) -> dict[tuple[date, str, str], float]:
    """Straight-line distance between each shape's own start and end point.

    Small = loop route, large = point-to-point. Static route geometry.
    """
    rows = conn.execute(
        """
        SELECT feed_version_date, line_number, shape_id,
               ST_Distance(
                   ST_StartPoint(line_geom_proj), ST_EndPoint(line_geom_proj)
               )
        FROM scratch.route_shape_geoms
        """
    ).fetchall()
    return {(r[0], r[1], r[2]): r[3] for r in rows}
