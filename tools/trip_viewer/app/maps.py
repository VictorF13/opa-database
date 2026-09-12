"""Folium (Leaflet) map builder for one trip: 4 independently toggleable layers.

Uses Folium/Leaflet, matching this repo's other labeling UIs
(`ml/trip_validity_model/app/maps.py`) rather than pydeck/deck.gl.

Tiles: "CartoDB Positron" was tried first for a muted background, but its
basemaps.cartocdn.com service started showing an "API key required"
watermark under normal interactive use (panning/zooming fires many tile
requests, which trips CARTO's free-tier rate limit). Rather than pull in
a different named provider, this uses the exact same plain OpenStreetMap
tiles as `ml/trip_validity_model/app/maps.py` (no key, no named
commercial provider, no rate-limit wall) and mutes them client-side
instead: Leaflet's tile layer accepts a `className` option applied to
every `<img>` tile it renders, so a partial `saturate()` +
brightness/contrast CSS filter on that class - plus a bit of layer
opacity - softens the busy default OSM colors without fully committing
to grayscale.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import folium
import pandas as pd

_FORTALEZA_LATLON = (-3.7319, -38.5267)
_TILE_OPACITY = 0.8
_TILE_CLASS = "trip-viewer-muted-tiles"
_TILE_FILTER_CSS = f"""
<style>
  .{_TILE_CLASS} {{ filter: saturate(0.4) brightness(1.08) contrast(0.9); }}
</style>
"""

_ROUTE_COLOR = "#000000"
_ROUTE_WEIGHT_PX = 6
_MIN_POINTS_TO_FIT_BOUNDS = 2

_CONFIDENCE_COLORS = {
    "high": "#1d4ed8",  # azul
    "medium": "#60a5fa",  # azul mais claro
    "low": "#bfdbfe",  # azul bem claro
}
_CONFIDENCE_LABELS_PT = {"high": "alta", "medium": "média", "low": "baixa"}
_NO_ESTIMATE_COLOR = "#ffffff"  # branco: sem estimativa de chegada
_STOP_BADGE_PX = 22

_POSITION_ARROW_PX = 18
_FARE_COLOR = "#eab308"  # amarelo
_FARE_SIZE_PX = 14

# Small inline-HTML swatches mirroring each layer's actual map symbol,
# prepended to that layer's name so the layer control reads as a legend
# instead of plain checkboxes. `folium.FeatureGroup(name=...)` is passed
# straight through to Leaflet's `L.Control.Layers`, which sets it via
# `label.innerHTML`, so an HTML string here renders as real markup.
_LEGEND_ROUTE_SWATCH = (
    "<span style='display:inline-block;width:16px;height:4px;"
    f"background:{_ROUTE_COLOR};vertical-align:middle;margin-left:6px;'></span>"
)
_LEGEND_STOP_SWATCH = (
    "<span style='display:inline-block;width:12px;height:12px;border-radius:50%;"
    f"background:{_CONFIDENCE_COLORS['high']};border:1.5px solid #1e293b;"
    "vertical-align:middle;margin-left:6px;'></span>"
)
_LEGEND_POSITION_SWATCH = (
    "<span style='display:inline-block;width:14px;height:14px;"
    "vertical-align:middle;margin-left:6px;'>"
    "<svg width='14' height='14' viewBox='0 0 18 18'>"
    "<polygon points='9,1 16,17 9,12.5 2,17' fill='#ffffff' stroke='#000000'"
    " stroke-width='1.5'/></svg></span>"
)
_LEGEND_FARE_SWATCH = (
    "<span style='display:inline-block;width:12px;height:12px;"
    f"background:{_FARE_COLOR};border:1.5px solid #1e293b;"
    "vertical-align:middle;margin-left:6px;'></span>"
)


def _legend_name(swatch: str, text: str) -> str:
    """Put text first, swatch trailing, so the icon lands on the right."""
    return f"{text}{swatch}"


def _format_hms(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


def _elapsed_label(ts: datetime | None, trip_start: datetime | None) -> str:
    if ts is None or trip_start is None:
        return "desconhecido"
    return _format_hms((ts - trip_start).total_seconds())


def _stop_icon(stop_sequence: int, color: str) -> folium.DivIcon:
    text_color = "#0f172a" if color != _CONFIDENCE_COLORS["high"] else "#ffffff"
    html = f"""
    <div style="
        width:{_STOP_BADGE_PX}px; height:{_STOP_BADGE_PX}px; border-radius:50%;
        background:{color}; border:1.5px solid #1e293b; color:{text_color};
        display:flex; align-items:center; justify-content:center;
        font-size:10px; font-weight:700; font-family:sans-serif;">
        {stop_sequence}
    </div>
    """
    return folium.DivIcon(
        html=html,
        icon_size=(_STOP_BADGE_PX, _STOP_BADGE_PX),
        icon_anchor=(_STOP_BADGE_PX // 2, _STOP_BADGE_PX // 2),
    )


def _arrow_icon(direction_deg: float) -> folium.DivIcon:
    """Build a small white-fill/black-outline triangle, rotated to a compass bearing."""
    html = f"""
    <div style="transform: rotate({direction_deg}deg);
                width:{_POSITION_ARROW_PX}px; height:{_POSITION_ARROW_PX}px;">
        <svg width="{_POSITION_ARROW_PX}" height="{_POSITION_ARROW_PX}"
             viewBox="0 0 18 18">
            <polygon points="9,1 16,17 9,12.5 2,17" fill="#ffffff"
                     stroke="#000000" stroke-width="1.5"/>
        </svg>
    </div>
    """
    return folium.DivIcon(
        html=html,
        icon_size=(_POSITION_ARROW_PX, _POSITION_ARROW_PX),
        icon_anchor=(_POSITION_ARROW_PX // 2, _POSITION_ARROW_PX // 2),
    )


def _fare_icon(*, exact: bool) -> folium.DivIcon:
    """Build a small yellow square, dimmer when the location is interpolated."""
    opacity = 1 if exact else 0.55
    html = f"""
    <div style="
        width:{_FARE_SIZE_PX}px; height:{_FARE_SIZE_PX}px;
        background:{_FARE_COLOR}; border:1.5px solid #1e293b; opacity:{opacity};">
    </div>
    """
    return folium.DivIcon(
        html=html,
        icon_size=(_FARE_SIZE_PX, _FARE_SIZE_PX),
        icon_anchor=(_FARE_SIZE_PX // 2, _FARE_SIZE_PX // 2),
    )


def _add_route_layer(
    fmap: folium.Map, shape: pd.DataFrame
) -> list[tuple[float, float]]:
    fg = folium.FeatureGroup(
        name=_legend_name(_LEGEND_ROUTE_SWATCH, "Trajeto da linha"), show=True
    )
    coords = list(zip(shape["latitude"], shape["longitude"], strict=True))
    if coords:
        folium.PolyLine(
            locations=coords, color=_ROUTE_COLOR, weight=_ROUTE_WEIGHT_PX, opacity=0.9
        ).add_to(fg)
        folium.Marker(
            location=coords[0],
            icon=folium.Icon(color="green", icon="flag", prefix="fa"),
            tooltip="Início do trajeto",
        ).add_to(fg)
        folium.Marker(
            location=coords[-1],
            icon=folium.Icon(color="black", icon="flag-checkered", prefix="fa"),
            tooltip="Fim do trajeto",
        ).add_to(fg)
    fg.add_to(fmap)
    return coords


def _add_stops_layer(
    fmap: folium.Map, stops: pd.DataFrame
) -> list[tuple[float, float]]:
    fg = folium.FeatureGroup(
        name=_legend_name(_LEGEND_STOP_SWATCH, "Paradas"), show=True
    )
    coords = []
    for row in stops.itertuples():
        confidence = row.confidence
        color = _CONFIDENCE_COLORS.get(confidence, _NO_ESTIMATE_COLOR)
        if pd.notna(row.arrival_time):
            confidence_pt = _CONFIDENCE_LABELS_PT.get(confidence, confidence)
            arrival_line = (
                f"Chegada estimada: {row.arrival_time:%H:%M:%S} "
                f"(confiança {confidence_pt}, intervalo entre pings de "
                f"{row.ping_gap_seconds}s)"
            )
        else:
            arrival_line = "Sem estimativa de chegada"
        tooltip = folium.Tooltip(
            f"<b>#{row.stop_sequence} - {row.stop_id}</b><br>"
            f"{row.stop_name or ''}<br>{arrival_line}"
        )
        folium.Marker(
            location=(row.latitude, row.longitude),
            icon=_stop_icon(row.stop_sequence, color),
            tooltip=tooltip,
        ).add_to(fg)
        coords.append((row.latitude, row.longitude))
    fg.add_to(fmap)
    return coords


def _add_positions_layer(
    fmap: folium.Map, positions: pd.DataFrame, trip_start: datetime | None
) -> list[tuple[float, float]]:
    fg = folium.FeatureGroup(
        name=_legend_name(_LEGEND_POSITION_SWATCH, "Posições do ônibus"), show=True
    )
    coords = []
    for row in positions.itertuples():
        elapsed = _elapsed_label(row.ping_at, trip_start)
        speed = "desconhecida" if row.speed is None else f"{row.speed} km/h"
        direction = "desconhecida" if row.direction is None else f"{row.direction}°"
        tooltip = folium.Tooltip(
            f"<b>{row.ping_at:%H:%M:%S}</b> (+{elapsed} desde o início da viagem)<br>"
            f"Velocidade: {speed}<br>Direção: {direction}"
        )
        if row.direction is not None:
            folium.Marker(
                location=(row.latitude, row.longitude),
                icon=_arrow_icon(row.direction),
                tooltip=tooltip,
            ).add_to(fg)
        else:
            folium.CircleMarker(
                location=(row.latitude, row.longitude),
                radius=5,
                color="#000000",
                weight=1.5,
                fill=True,
                fill_color="#ffffff",
                fill_opacity=1,
                tooltip=tooltip,
            ).add_to(fg)
        coords.append((row.latitude, row.longitude))
    fg.add_to(fmap)
    return coords


def _add_fares_layer(
    fmap: folium.Map, fares: pd.DataFrame, trip_start: datetime | None
) -> list[tuple[float, float]]:
    fg = folium.FeatureGroup(
        name=_legend_name(_LEGEND_FARE_SWATCH, "Tarifas"), show=True
    )
    coords = []
    for row in fares.itertuples():
        if pd.isna(row.latitude) or pd.isna(row.longitude):
            continue
        elapsed = _elapsed_label(row.boarding_at, trip_start)
        location_note = "exata" if row.location_is_exact else "interpolada"
        tooltip = folium.Tooltip(
            f"<b>Tarifa {row.fare_id}</b><br>"
            f"Tipo de passageiro: {row.passenger_type_id}<br>"
            f"Embarque: {row.boarding_at:%H:%M:%S} "
            f"(+{elapsed} desde o início da viagem)<br>"
            f"Valor pago: R$ {row.fare_paid:.2f}<br>"
            f"Tipo de integração: {row.integration_type}<br>"
            f"Localização: {location_note}"
        )
        folium.Marker(
            location=(row.latitude, row.longitude),
            icon=_fare_icon(exact=row.location_is_exact),
            tooltip=tooltip,
        ).add_to(fg)
        coords.append((row.latitude, row.longitude))
    fg.add_to(fmap)
    return coords


def build_blank_map() -> folium.Map:
    """Build a plain, layer-free basemap, shown before any trip is loaded."""
    base_tiles = folium.TileLayer(
        "OpenStreetMap",
        control=False,
        opacity=_TILE_OPACITY,
        className=_TILE_CLASS,
    )
    fmap = folium.Map(location=_FORTALEZA_LATLON, zoom_start=13, tiles=base_tiles)
    fmap.get_root().header.add_child(folium.Element(_TILE_FILTER_CSS))
    return fmap


def build_trip_map(
    meta: dict[str, Any],
    stops: pd.DataFrame,
    positions: pd.DataFrame,
    fares: pd.DataFrame,
    shape: pd.DataFrame,
) -> folium.Map:
    """Build the single trip map with 4 independently toggleable layers.

    Args:
        meta: This trip's `db.fetch_trip_meta` row (used for `trip_start_timestamp`,
            to compute "elapsed since trip start" in tooltips).
        stops: `db.fetch_stops` result.
        positions: `db.fetch_positions` result (AVL pings only).
        fares: `db.fetch_fares` result.
        shape: `db.fetch_shape` result.

    Returns:
        A Folium map fit to the union of all plotted points, with a
        top-right layer control (Trajeto da linha / Paradas / Posições do
        ônibus / Tarifas), ready for `streamlit_folium.st_folium`.

    """
    fmap = build_blank_map()

    trip_start = meta.get("trip_start_timestamp")
    all_coords: list[tuple[float, float]] = []
    all_coords += _add_route_layer(fmap, shape)
    all_coords += _add_stops_layer(fmap, stops)
    all_coords += _add_positions_layer(fmap, positions, trip_start)
    all_coords += _add_fares_layer(fmap, fares, trip_start)

    if len(all_coords) >= _MIN_POINTS_TO_FIT_BOUNDS:
        lats = [c[0] for c in all_coords]
        lons = [c[1] for c in all_coords]
        fmap.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])

    folium.LayerControl(collapsed=False, position="topright").add_to(fmap)
    return fmap
