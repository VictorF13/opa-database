"""Folium (Leaflet) map builders: one map per GTFS direction, with a graded AVL trail.

Uses Folium/Leaflet rather than pydeck/deck.gl: this repo's other
active-learning labelers (`tools/trip_finder`, `tools/vehicle_identity_labeler`
on the `explore/vehicle-trip-matching` branch) already render OpenStreetMap
tiles successfully via Leaflet.js, whereas deck.gl's `TileLayer` needs a
`render_sub_layers` callback to actually composite raster tiles that
Python can't easily express — without it, the tiles fetch but never draw,
which is what produced solid-black maps.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import folium

if TYPE_CHECKING:
    from collections.abc import Sequence

_FORTALEZA_LATLON = (-3.7319, -38.5267)

_ROUTE_COLOR = "#1e64c8"
_ROUTE_WEIGHT_PX = 6
_START_COLOR = "#00aa00"
_END_COLOR = "#dc0000"
_MARKER_RADIUS_PX = 8
_TRAIL_RADIUS_PX = 6
_TRAIL_OUTLINE_COLOR = "#000000"
_TRAIL_OUTLINE_WEIGHT_PX = 1.5
_MIN_POINTS_TO_FIT_BOUNDS = 2


def _grayscale_trail(
    positions: Sequence[tuple[float, float, str]],
) -> list[tuple[float, float, str, str]]:
    """Turn `(lon, lat, iso_timestamp)` pings into `(lat, lon, hex_color, timestamp)`.

    Earliest ping is white, latest is black, everything else interpolated
    linearly in between.
    """
    if not positions:
        return []
    timestamps = [datetime.fromisoformat(ts) for _, _, ts in positions]
    t_min, t_max = min(timestamps), max(timestamps)
    span = (t_max - t_min).total_seconds() or 1.0
    points = []
    for (lon, lat, ts), t in zip(positions, timestamps, strict=True):
        fraction = (t - t_min).total_seconds() / span
        shade = round(255 * (1 - fraction))
        points.append((lat, lon, f"#{shade:02x}{shade:02x}{shade:02x}", ts))
    return points


def build_direction_map(
    shape_coordinates: list[list[float]] | None,
    positions: Sequence[tuple[float, float, str]],
) -> folium.Map:
    """Build one direction's map: route line + start/end markers + AVL trail.

    Args:
        shape_coordinates: `[lon, lat]` pairs for the GTFS shape
            LineString, or `None` if this direction has no matched
            shape.
        positions: `(lon, lat, iso_timestamp)` AVL pings for this trip,
            ordered by time.

    Returns:
        A Folium map, already fit to the data's bounding box (no fixed
        zoom guess needed), ready for `streamlit_folium.st_folium`.

    """
    trail = _grayscale_trail(positions)
    shape_latlon = [(lat, lon) for lon, lat in shape_coordinates or []]
    trail_latlon = [(lat, lon) for lat, lon, _, _ in trail]
    all_points = shape_latlon + trail_latlon

    center = list(all_points[0]) if all_points else list(_FORTALEZA_LATLON)
    fmap = folium.Map(
        location=center,
        zoom_start=15 if len(all_points) < _MIN_POINTS_TO_FIT_BOUNDS else 13,
    )

    if shape_latlon:
        folium.PolyLine(
            locations=shape_latlon,
            color=_ROUTE_COLOR,
            weight=_ROUTE_WEIGHT_PX,
            opacity=0.85,
        ).add_to(fmap)
        # tooltip=<str> only shows on hover; permanent=True keeps the
        # START/END label visible on the map at all times.
        folium.CircleMarker(
            location=shape_latlon[0],
            radius=_MARKER_RADIUS_PX,
            color=_TRAIL_OUTLINE_COLOR,
            weight=1,
            fill=True,
            fill_color=_START_COLOR,
            fill_opacity=1,
            tooltip=folium.Tooltip("START", permanent=True, direction="top"),
        ).add_to(fmap)
        folium.CircleMarker(
            location=shape_latlon[-1],
            radius=_MARKER_RADIUS_PX,
            color=_TRAIL_OUTLINE_COLOR,
            weight=1,
            fill=True,
            fill_color=_END_COLOR,
            fill_opacity=1,
            tooltip=folium.Tooltip("END", permanent=True, direction="top"),
        ).add_to(fmap)

    # Added last so it paints over the route line and start/end markers
    # (Leaflet draws in add-order, later = on top, same as deck.gl).
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
