"""Local (shapely-based) reimplementation of the trip_labeler PostGIS metrics.

Mirrors notebooks/_scoring_lib.py::_CANDIDATE_SCORING_SELECT exactly (same
distance/line-fraction/progress-correlation/start-end-proximity formulas),
but computed with shapely 2.x vectorized ops against locally-loaded shape
geometry instead of per-row PostGIS calls. find_candidates.py spot-checks a
random sample of this module's output against live PostGIS so the two can
never silently drift apart -- see spot_check_direction_metrics.

Kept separate from tools/trip_labeler/scoring.py: that module's Candidate/
compute_feature_vector shape (best-candidate + gap-to-runner-up) is specific
to the *shape-direction* classification problem trip_labeler solves. This
module solves a different problem (which *vehicle_id* performed a trip) and
needs both directions' metrics side by side, not collapsed into one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import TYPE_CHECKING

import numpy as np
import shapely

if TYPE_CHECKING:
    from datetime import date

    import psycopg
    import pyproj

UTM_24S = 32724
FORTALEZA_OFFSET = timedelta(hours=-3)  # UTC-3, no DST since 2008 (see CLAUDE.md)
DIRECTIONS = ("I", "V")  # ida, volta -- see shape_id suffix convention below
MIN_PINGS_FOR_CORR = 2


@dataclass
class ShapeGeom:
    """One direction's shape geometry, projected to UTM 24S (meters)."""

    shape_id: str
    line: shapely.LineString
    start_point: shapely.Point
    end_point: shapely.Point
    length_m: float


@dataclass
class DirectionMetrics:
    """Per-(trip, candidate vehicle, direction) metrics, NaN when not computable."""

    n_pings_in_window: int
    avg_dist_to_line_m: float
    progress_corr: float
    start_proximity_m: float
    end_proximity_m: float


@dataclass
class MovementMetrics:
    """Per-(trip, candidate vehicle) raw movement metrics, independent of any shape.

    Unlike DirectionMetrics (distance/progress relative to a specific ida/
    volta route), these describe the candidate's own GPS trace on its own
    terms -- did it actually go anywhere, for how long, and how spread out
    -- which is exactly the signal missing to tell "genuinely idle/parked
    near the line" apart from "moving, but along some other route" using
    distance-to-line alone.
    """

    total_distance_m: float
    ping_timespan_sec: float
    spatial_dispersion_m: float


def load_shapes(
    conn: psycopg.Connection, pairs: list[tuple[date, str]]
) -> dict[tuple[date, str, str], ShapeGeom]:
    """Load shape geometry for exactly the (feed_version_date, line_number) pairs.

    Shape identity uses the shape_id suffix (-I/-V), not direction_id -- every
    row in scratch.route_shape_geoms has direction_id NULL (verified), but the
    suffix convention is 100% consistent (16588 -I / 16581 -V, no exceptions).

    Args:
        conn: Open psycopg connection.
        pairs: (feed_version_date, line_number) pairs to load, deduplicated by
            caller.

    Returns:
        dict[(feed_version_date, line_number, "I"|"V"), ShapeGeom]

    """
    if not pairs:
        return {}
    feeds = [p[0] for p in pairs]
    lines = [p[1] for p in pairs]
    rows = conn.execute(
        """
        SELECT g.feed_version_date, g.line_number, g.shape_id, g.shape_length_m,
               ST_AsBinary(g.line_geom_proj)
        FROM scratch.route_shape_geoms g
        JOIN unnest(%(feeds)s::date[], %(lines)s::text[]) AS want(feed, line)
          ON g.feed_version_date = want.feed AND g.line_number = want.line
        """,
        {"feeds": feeds, "lines": lines},
    ).fetchall()
    out: dict[tuple[date, str, str], ShapeGeom] = {}
    for feed_version_date, line_number, shape_id, length_m, wkb in rows:
        direction = shape_id[-1]  # "I" or "V"
        line = shapely.from_wkb(bytes(wkb))
        out[(feed_version_date, line_number, direction)] = ShapeGeom(
            shape_id=shape_id,
            line=line,
            start_point=shapely.get_point(line, 0),
            end_point=shapely.get_point(line, -1),
            length_m=length_m,
        )
    return out


def iv_overlap_m(
    shapes: dict[tuple[date, str, str], ShapeGeom],
) -> dict[tuple[date, str], float]:
    """Hausdorff distance between a line's -I and -V shapes, computed locally.

    Same quantity as trip_labeler's load_iv_overlap (ST_HausdorffDistance),
    recomputed with shapely instead of a Postgres round-trip since the
    geometry is already loaded locally.
    """
    out: dict[tuple[date, str], float] = {}
    keys = {(f, ln) for f, ln, _d in shapes}
    for feed_version_date, line_number in keys:
        i = shapes.get((feed_version_date, line_number, "I"))
        v = shapes.get((feed_version_date, line_number, "V"))
        if i is not None and v is not None:
            out[(feed_version_date, line_number)] = shapely.hausdorff_distance(
                i.line, v.line
            )
    return out


def shape_start_end_dist_m(
    shapes: dict[tuple[date, str, str], ShapeGeom],
) -> dict[tuple[date, str, str], float]:
    """Straight-line distance between each shape's own start and end point."""
    return {
        key: shapely.distance(g.start_point, g.end_point) for key, g in shapes.items()
    }


def project_pings(
    longitude: np.ndarray, latitude: np.ndarray, transformer: pyproj.Transformer
) -> tuple[np.ndarray, np.ndarray]:
    """Project WGS84 lon/lat arrays to UTM 24S x/y arrays via a pyproj Transformer.

    Args:
        longitude: Raw longitude values (EPSG:4326).
        latitude: Raw latitude values (EPSG:4326).
        transformer: A pyproj.Transformer(always_xy=True) from EPSG:4326 to
            EPSG:32724, built once by the caller and reused across all pings.

    Returns:
        (x, y): UTM 24S meter coordinates, same shape as the inputs.

    """
    x, y = transformer.transform(longitude, latitude)
    return np.asarray(x), np.asarray(y)


def compute_direction_metrics(
    ping_x: np.ndarray,
    ping_y: np.ndarray,
    ping_epoch_sec: np.ndarray,
    shape: ShapeGeom | None,
) -> DirectionMetrics:
    """Compute one direction's metrics for one (trip, candidate vehicle) window.

    Mirrors _CANDIDATE_SCORING_SELECT's scored_pings CTE + aggregate exactly:
    avg_dist_to_line_m/progress_corr/start_proximity_m/end_proximity_m. Pings
    must already be filtered to this trip's [trip_opened_at, trip_closed_at]
    window and this candidate's vehicle_id, but need NOT be pre-sorted by
    time -- this function sorts them itself (first/last-by-time is exactly
    what start/end proximity need).

    Args:
        ping_x: UTM 24S x-coordinates of this candidate's pings in the window.
        ping_y: UTM 24S y-coordinates, same order as ping_x.
        ping_epoch_sec: Unix timestamps (seconds), same order as ping_x.
        shape: The direction's shape geometry, or None if that direction has
            no resolved shape for this trip's feed (rare, see load_shapes).

    Returns:
        DirectionMetrics with NaN fields wherever the underlying PostGIS
        aggregate would also have been NULL (e.g. zero pings, or a single
        ping making CORR undefined).

    """
    n = len(ping_x)
    if shape is None or n == 0:
        return DirectionMetrics(
            n_pings_in_window=n,
            avg_dist_to_line_m=np.nan,
            progress_corr=np.nan,
            start_proximity_m=np.nan,
            end_proximity_m=np.nan,
        )

    order = np.argsort(ping_epoch_sec, kind="mergesort")
    x, y, t = ping_x[order], ping_y[order], ping_epoch_sec[order]
    pts = shapely.points(x, y)

    dist = shapely.distance(pts, shape.line)
    avg_dist = float(np.mean(dist))

    if n >= MIN_PINGS_FOR_CORR:
        frac = shapely.line_locate_point(shape.line, pts, normalized=True)
        if np.std(t) > 0 and np.std(frac) > 0:
            progress_corr = float(np.corrcoef(t, frac)[0, 1])
        else:
            progress_corr = np.nan
    else:
        progress_corr = np.nan

    start_proximity = float(shapely.distance(pts[0], shape.start_point))
    end_proximity = float(shapely.distance(pts[-1], shape.end_point))

    return DirectionMetrics(
        n_pings_in_window=n,
        avg_dist_to_line_m=avg_dist,
        progress_corr=progress_corr,
        start_proximity_m=start_proximity,
        end_proximity_m=end_proximity,
    )


def compute_movement_metrics(
    ping_x: np.ndarray, ping_y: np.ndarray, ping_epoch_sec: np.ndarray
) -> MovementMetrics:
    """Compute one candidate's own raw movement metrics, independent of any shape.

    Canonical scalar reference for find_candidates.py's vectorized per-group
    equivalent -- same role as compute_direction_metrics, see its docstring.

    Args:
        ping_x: UTM 24S x-coordinates of this candidate's pings in the window.
        ping_y: UTM 24S y-coordinates, same order as ping_x.
        ping_epoch_sec: Unix timestamps (seconds), same order as ping_x.

    Returns:
        MovementMetrics with NaN fields wherever undefined (e.g. a single
        ping has no timespan/distance to speak of).

    """
    n = len(ping_x)
    if n == 0:
        return MovementMetrics(
            total_distance_m=np.nan,
            ping_timespan_sec=np.nan,
            spatial_dispersion_m=np.nan,
        )

    order = np.argsort(ping_epoch_sec, kind="mergesort")
    x, y, t = ping_x[order], ping_y[order], ping_epoch_sec[order]

    if n >= MIN_PINGS_FOR_CORR:
        step_dist = np.hypot(np.diff(x), np.diff(y))
        total_distance = float(np.sum(step_dist))
        timespan = float(t[-1] - t[0])
    else:
        total_distance = np.nan
        timespan = np.nan

    # radius of gyration: RMS distance from the centroid, a standard O(n)
    # proxy for "how spread out are these points" -- the same underlying
    # idea as mean pairwise distance, without the O(n^2) cost of literally
    # computing every pair (a real cost at up to ~1500 pings/candidate).
    centroid_x, centroid_y = float(np.mean(x)), float(np.mean(y))
    sq_dist = (x - centroid_x) ** 2 + (y - centroid_y) ** 2
    dispersion = float(np.sqrt(np.mean(sq_dist)))

    return MovementMetrics(
        total_distance_m=total_distance,
        ping_timespan_sec=timespan,
        spatial_dispersion_m=dispersion,
    )


def local_fortaleza(dt):  # noqa: ANN001, ANN201
    """Convert a UTC-aware datetime to naive Fortaleza local time (UTC-3, no DST)."""
    return (dt.astimezone(UTC) + FORTALEZA_OFFSET).replace(tzinfo=None)
