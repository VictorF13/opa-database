"""Folium map builders for the Bus Matching labeler.

One small map per candidate device -- each map shows both GTFS
directions (I in blue, V in orange, when both exist) plus that one
candidate's own AVL trail, gradient-shaded white (trip start) to black
(trip end) so direction and dwell time read at a glance, matching
`trip_validity_model/app/maps.py`'s convention. Candidates are
distinguished by their device id label and score/origin badge in the
app, not by color -- no per-candidate color coding here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import folium

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pandas as pd

_FORTALEZA_LATLON = (-3.7319, -38.5267)

# CartoDB Positron: a light, near-monochrome basemap. The default OSM
# tiles are busy/saturated enough that they visually compete with the
# route line and AVL trail on top of them -- confirmed as the actual
# "fighting the lines" complaint, not a rendering bug.
_BASEMAP = "CartoDB positron"

_DIRECTION_COLORS = {"I": "#1e64c8", "V": "#e8820c"}
_ROUTE_WEIGHT_PX = 7
_ROUTE_OPACITY = 0.9
_STOP_COLOR = "#666666"
_STOP_RADIUS_PX = 3
_START_COLOR = "#00aa00"
_END_COLOR = "#dc0000"
_ENDPOINT_RADIUS_PX = 9
_TRAIL_RADIUS_PX = 6
_TRAIL_OUTLINE_COLOR = "#000000"
_TRAIL_OUTLINE_WEIGHT_PX = 1.5
_MIN_POINTS_TO_FIT_BOUNDS = 2


def _grayscale_trail(track: pd.DataFrame) -> list[tuple[float, float, str, str]]:
    """Turn a time-ordered track into `(lat, lon, hex_color, iso_ts)` points."""
    if track.empty:
        return []
    timestamps = list(track["metric_timestamp"])
    t_min, t_max = min(timestamps), max(timestamps)
    span = (t_max - t_min).total_seconds() or 1.0
    points = []
    for lat, lon, ts in zip(
        track["latitude"], track["longitude"], timestamps, strict=True
    ):
        fraction = (ts - t_min).total_seconds() / span
        shade = round(255 * (1 - fraction))
        points.append((lat, lon, f"#{shade:02x}{shade:02x}{shade:02x}", ts.isoformat()))
    return points


def _add_route_and_stops(
    fmap: folium.Map,
    direction: str,
    shape_latlon: list[tuple[float, float]],
    stops: pd.DataFrame,
) -> None:
    route_color = _DIRECTION_COLORS.get(direction, "#444444")
    if shape_latlon:
        folium.PolyLine(
            locations=shape_latlon,
            color=route_color,
            weight=_ROUTE_WEIGHT_PX,
            opacity=_ROUTE_OPACITY,
            tooltip=f"direction {direction}",
        ).add_to(fmap)
        folium.CircleMarker(
            location=shape_latlon[0],
            radius=_ENDPOINT_RADIUS_PX,
            color=_TRAIL_OUTLINE_COLOR,
            weight=1,
            fill=True,
            fill_color=_START_COLOR,
            fill_opacity=1,
            tooltip=folium.Tooltip(
                f"{direction} START", permanent=True, direction="top"
            ),
        ).add_to(fmap)
        folium.CircleMarker(
            location=shape_latlon[-1],
            radius=_ENDPOINT_RADIUS_PX,
            color=_TRAIL_OUTLINE_COLOR,
            weight=1,
            fill=True,
            fill_color=_END_COLOR,
            fill_opacity=1,
            tooltip=folium.Tooltip(f"{direction} END", permanent=True, direction="top"),
        ).add_to(fmap)

    for stop in stops.itertuples(index=False):
        folium.CircleMarker(
            location=(stop.stop_lat, stop.stop_lon),
            radius=_STOP_RADIUS_PX,
            color=_STOP_COLOR,
            weight=1,
            fill=True,
            fill_color=_STOP_COLOR,
            fill_opacity=0.7,
            tooltip=f"stop {stop.stop_id} (dir {direction})",
        ).add_to(fmap)


def build_candidate_map(
    shapes: Mapping[str, tuple[list[list[float]] | None, pd.DataFrame]],
    track: pd.DataFrame,
) -> folium.Map:
    """Build one candidate's own small map: both directions + its AVL trail.

    Args:
        shapes: `{"I": (geojson_coords, stops), "V": (...)}`, only the
            directions that actually exist in GTFS for this trip's
            route -- direction I drawn in blue, V in orange, so a
            genuinely divergent I/V pair (not just the same corridor
            reversed) is visible on one map instead of assumed away.
        track: This candidate's AVL positions in the trip window --
            columns `metric_timestamp`, `latitude`, `longitude`.

    Returns:
        A Folium map fit to the union of the shape and the trail.

    """
    trail = _grayscale_trail(track)
    trail_latlon = [(lat, lon) for lat, lon, _, _ in trail]
    all_points = list(trail_latlon)

    center = list(_FORTALEZA_LATLON)
    for coords, _stops in shapes.values():
        if coords:
            center = [coords[0][1], coords[0][0]]
            break
    if trail_latlon:
        center = list(trail_latlon[0])
    fmap = folium.Map(location=center, zoom_start=13, tiles=_BASEMAP)

    for direction, (coords, stops) in shapes.items():
        shape_latlon = [(lat, lon) for lon, lat in coords or []]
        all_points.extend(shape_latlon)
        _add_route_and_stops(fmap, direction, shape_latlon, stops)

    for lat, lon, color, timestamp in trail:
        folium.CircleMarker(
            location=(lat, lon),
            radius=_TRAIL_RADIUS_PX,
            color=_TRAIL_OUTLINE_COLOR,
            weight=_TRAIL_OUTLINE_WEIGHT_PX,
            fill=True,
            fill_color=color,
            fill_opacity=0.9,
            tooltip=timestamp,
        ).add_to(fmap)

    if len(all_points) >= _MIN_POINTS_TO_FIT_BOUNDS:
        lats = [p[0] for p in all_points]
        lons = [p[1] for p in all_points]
        fmap.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])

    return fmap
