"""In-memory GTFS shape cache for the Bus Matching model.

Builds, once per process, a dict keyed by `(feed_version_date, shape_id)`
holding densified numpy arrays for fast linear referencing. Every
point-to-route projection downstream (chainage, offset, endpoint/stop
distances) is pure numpy against this cache, never a per-trip PostGIS
round trip -- there are only a few hundred `(feed_version_date,
shape_id)` combinations, so the whole cache fits comfortably in memory
and rebuilding it takes seconds.

All coordinates are in the same metric CRS as
`ml.trip_validity_route_shapes.shape_geom_metric` (SRID 31984), so
distances are already in meters with no reprojection needed downstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import datetime

    import psycopg

ShapeKey = tuple["datetime.date", str]


@dataclass(frozen=True)
class RouteShape:
    """Precomputed linear-referencing arrays for one GTFS shape.

    Attributes:
        feed_version_date: The GTFS feed snapshot this shape belongs to.
        shape_id: GTFS shape id, e.g. `"shape0051-I"`.
        route_short_name: The route's short name (matches
            `ml.trip_validity_final.route_id` once zero-padded).
        direction: `"I"` or `"V"`.
        points: `(n, 2)` ordered shape vertices, metric xy.
        seg_start: `(n-1, 2)` segment start points.
        seg_end: `(n-1, 2)` segment end points.
        seg_vec: `(n-1, 2)` segment vectors (`seg_end - seg_start`).
        seg_len: `(n-1,)` segment lengths in meters.
        seg_cum_start: `(n-1,)` cumulative length along the route at each
            segment's start, i.e. the chainage of `seg_start[i]`.
        total_length: Total route length in meters.
        start_point: `(2,)` the shape's first vertex.
        end_point: `(2,)` the shape's last vertex.
        bbox: `(minx, miny, maxx, maxy)`.
        stop_points: `(m, 2)` every distinct stop along this shape, union
            of all `route_stops` variants -- for the stop-coincidence
            feature. Empty (`shape (0, 2)`) if no stops matched.
        stop_ids: `stop_id` for each row of `stop_points`, same order.
        first_stop_points: `(k, 2)` union, across all variants, of the
            stop at that variant's minimum `stop_sequence`. Variants can
            genuinely start at different physical stops.
        last_stop_points: `(k, 2)` same, for each variant's maximum
            `stop_sequence`.

    """

    feed_version_date: datetime.date
    shape_id: str
    route_short_name: str
    direction: str
    points: np.ndarray
    seg_start: np.ndarray
    seg_end: np.ndarray
    seg_vec: np.ndarray
    seg_len: np.ndarray
    seg_cum_start: np.ndarray
    total_length: float
    start_point: np.ndarray
    end_point: np.ndarray
    bbox: tuple[float, float, float, float]
    stop_points: np.ndarray
    stop_ids: list[str]
    first_stop_points: np.ndarray
    last_stop_points: np.ndarray


_SHAPES_QUERY = """
    SELECT
        s.feed_version_date,
        s.shape_id,
        s.route_short_name,
        s.direction,
        (dp).path[1] AS pt_order,
        ST_X((dp).geom) AS x,
        ST_Y((dp).geom) AS y
    FROM ml.trip_validity_route_shapes s,
         LATERAL ST_DumpPoints(s.shape_geom_metric) AS dp
    ORDER BY s.feed_version_date, s.shape_id, pt_order;
"""

_STOPS_QUERY = """
    WITH shape_stops AS (
        SELECT
            feed_version_date,
            shape_id,
            variant_id,
            stop_sequence,
            stop_id,
            ST_X(ST_Transform(geom, 31984)) AS x,
            ST_Y(ST_Transform(geom, 31984)) AS y
        FROM ml.trip_validity_route_stops
    ),
    ranked AS (
        SELECT
            *,
            row_number() OVER (
                PARTITION BY feed_version_date, shape_id, variant_id
                ORDER BY stop_sequence
            ) AS rn_first,
            row_number() OVER (
                PARTITION BY feed_version_date, shape_id, variant_id
                ORDER BY stop_sequence DESC
            ) AS rn_last
        FROM shape_stops
    )
    SELECT
        feed_version_date,
        shape_id,
        stop_id,
        x,
        y,
        bool_or(rn_first = 1) AS is_first,
        bool_or(rn_last = 1) AS is_last
    FROM ranked
    GROUP BY feed_version_date, shape_id, stop_id, x, y;
"""


def build_shape_cache(conn: psycopg.Connection) -> dict[ShapeKey, RouteShape]:
    """Build the full in-memory GTFS shape cache.

    Args:
        conn: An open connection.

    Returns:
        Dict keyed by `(feed_version_date, shape_id)`. A trip whose
        `(gtfs_feed_version_date, gtfs_shape_id_i/v)` isn't a key here has
        no usable GTFS geometry for that direction and must be skipped,
        not scored -- confirmed live that ~4.2% of valid trips
        (30,132 / 720,080) reference a `shape_id` absent from their own
        feed's `route_shapes` rows, plus 6,833 more with no GTFS feed
        match at all.

    """
    shapes_by_key: dict[ShapeKey, dict[str, object]] = {}
    with conn.cursor() as cur:
        cur.execute(_SHAPES_QUERY)
        for (
            feed_version_date,
            shape_id,
            route_short_name,
            direction,
            _pt_order,
            x,
            y,
        ) in cur.fetchall():
            key = (feed_version_date, shape_id)
            entry = shapes_by_key.setdefault(
                key,
                {
                    "route_short_name": route_short_name,
                    "direction": direction,
                    "xs": [],
                    "ys": [],
                },
            )
            entry["xs"].append(x)
            entry["ys"].append(y)

    stops_by_key: dict[ShapeKey, dict[str, list]] = {}
    with conn.cursor() as cur:
        cur.execute(_STOPS_QUERY)
        for (
            feed_version_date,
            shape_id,
            stop_id,
            x,
            y,
            is_first,
            is_last,
        ) in cur.fetchall():
            key = (feed_version_date, shape_id)
            entry = stops_by_key.setdefault(
                key,
                {
                    "stop_ids": [],
                    "stop_xy": [],
                    "first_xy": [],
                    "last_xy": [],
                },
            )
            entry["stop_ids"].append(stop_id)
            entry["stop_xy"].append((x, y))
            if is_first:
                entry["first_xy"].append((x, y))
            if is_last:
                entry["last_xy"].append((x, y))

    cache: dict[ShapeKey, RouteShape] = {}
    for key, entry in shapes_by_key.items():
        points = np.column_stack(
            [
                np.asarray(entry["xs"], dtype=np.float64),
                np.asarray(entry["ys"], dtype=np.float64),
            ]
        )
        seg_start = points[:-1]
        seg_end = points[1:]
        seg_vec = seg_end - seg_start
        seg_len = np.linalg.norm(seg_vec, axis=1)
        seg_cum_start = np.concatenate([[0.0], np.cumsum(seg_len)[:-1]])
        total_length = float(seg_len.sum())

        stops = stops_by_key.get(
            key, {"stop_ids": [], "stop_xy": [], "first_xy": [], "last_xy": []}
        )
        stop_points = (
            np.asarray(stops["stop_xy"], dtype=np.float64)
            if stops["stop_xy"]
            else np.empty((0, 2), dtype=np.float64)
        )
        first_stop_points = (
            np.asarray(stops["first_xy"], dtype=np.float64)
            if stops["first_xy"]
            else np.empty((0, 2), dtype=np.float64)
        )
        last_stop_points = (
            np.asarray(stops["last_xy"], dtype=np.float64)
            if stops["last_xy"]
            else np.empty((0, 2), dtype=np.float64)
        )

        cache[key] = RouteShape(
            feed_version_date=key[0],
            shape_id=key[1],
            route_short_name=str(entry["route_short_name"]),
            direction=str(entry["direction"]),
            points=points,
            seg_start=seg_start,
            seg_end=seg_end,
            seg_vec=seg_vec,
            seg_len=seg_len,
            seg_cum_start=seg_cum_start,
            total_length=total_length,
            start_point=points[0],
            end_point=points[-1],
            bbox=(
                float(points[:, 0].min()),
                float(points[:, 1].min()),
                float(points[:, 0].max()),
                float(points[:, 1].max()),
            ),
            stop_points=stop_points,
            stop_ids=list(stops["stop_ids"]),
            first_stop_points=first_stop_points,
            last_stop_points=last_stop_points,
        )
    return cache


def project_points(shape: RouteShape, xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Project points onto a shape via vectorized nearest-segment search.

    For each input point, finds the closest point on any of the shape's
    segments and returns that point's chainage (distance along the
    route) and offset (perpendicular distance from the shape).

    Args:
        shape: The route shape to project onto.
        xy: `(k, 2)` points to project, same metric CRS as `shape`.

    Returns:
        `(chainage, offset)`, each `(k,)`. `chainage` is meters along the
        route from `shape.start_point`; `offset` is the perpendicular
        distance in meters from the nearest point on the shape.

    """
    # (k, n-1, 2): each point against every segment's start->vector.
    delta = xy[:, None, :] - shape.seg_start[None, :, :]
    seg_len_sq = np.clip(shape.seg_len**2, a_min=1e-12, a_max=None)
    t = np.clip(
        (delta * shape.seg_vec[None, :, :]).sum(axis=2) / seg_len_sq[None, :], 0.0, 1.0
    )
    # Squared distance from each point to its projection on each segment,
    # without materializing the (k, n-1, 2) "closest point" array and
    # without the dispatch overhead of np.linalg.norm -- this is the hot
    # path (called once per candidate pair per trip per direction), so
    # sqrt is only taken once per point below, not once per point-segment
    # pair.
    diff = delta - t[:, :, None] * shape.seg_vec[None, :, :]
    dist_sq = np.einsum("ijk,ijk->ij", diff, diff)
    best_seg = dist_sq.argmin(axis=1)
    rows = np.arange(xy.shape[0])
    offset = np.sqrt(dist_sq[rows, best_seg])
    chainage = (
        shape.seg_cum_start[best_seg] + t[rows, best_seg] * shape.seg_len[best_seg]
    )
    return chainage, offset


def min_distance_to_each(points: np.ndarray, candidates: np.ndarray) -> np.ndarray:
    """Vectorized min distance from each of `points` to any of `candidates`.

    Args:
        points: `(k, 2)` query points.
        candidates: `(m, 2)` reference points (e.g. stops).

    Returns:
        `(k,)` distances in meters; `inf` for every point when `candidates`
        is empty.

    """
    if candidates.shape[0] == 0 or points.shape[0] == 0:
        return np.full(points.shape[0], np.inf)
    diff = points[:, None, :] - candidates[None, :, :]
    dist_sq = np.einsum("ijk,ijk->ij", diff, diff)
    return np.sqrt(dist_sq.min(axis=1))
