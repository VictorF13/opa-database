"""Streamlit single-trip map viewer for the gold/diamond layers.

Not part of the packaged CLI, not production code -- a throwaway tool for
visually sanity-checking one trip at a time. Nothing is preloaded: data is
only queried once a trip_id is submitted.

Run with `uv run streamlit run tools/trip_viewer/app/streamlit_app.py`
from the repo root.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import db
import maps
import streamlit as st
from streamlit_folium import st_folium

if TYPE_CHECKING:
    import psycopg

st.set_page_config(page_title="Visualizador de Viagem", layout="wide")

# Both columns are pinned to this same pixel height (the left one via
# `st.container(height=...)`, the right one because that's what's passed
# to `st_folium`) so they visually line up regardless of how little the
# selector/info panel's own content needs.
PANEL_HEIGHT_PX = 650


@st.cache_resource
def get_connection() -> psycopg.Connection:
    """Open (and cache across reruns) the app's single database connection."""
    return db.get_connection()


def _labeled_value(label: str, value: object) -> None:
    st.caption(label)
    st.markdown(f"**{value}**")


def _render_trip_info(meta: dict) -> None:
    _labeled_value("Ônibus", meta["bus_id"])
    _labeled_value("Linha", f"{meta['route_id']} - {meta['route_name']}")
    _labeled_value("Sentido", meta["direction"])
    _labeled_value("Data do serviço", meta["trip_date"])
    _labeled_value("Início", meta["trip_start_timestamp"].strftime("%H:%M:%S"))
    _labeled_value("Término", meta["trip_end_timestamp"].strftime("%H:%M:%S"))


def _render_selector_panel(conn: psycopg.Connection) -> dict | None:
    """Render the trip picker and, once loaded, the trip info below it.

    Returns:
        The loaded trip's `db.fetch_trip_meta` row, or `None` if no trip
        is loaded yet / the typed trip_id doesn't exist.

    """
    st.subheader("Visualizador de Viagem")

    with st.form("trip_form", border=False):
        trip_id = st.number_input("ID da viagem", min_value=1, step=1, value=None)
        submitted = st.form_submit_button("Carregar viagem", width="stretch")

    if submitted and trip_id:
        st.session_state.trip_id = int(trip_id)

    if "trip_id" not in st.session_state:
        st.info("Digite um ID de viagem acima e clique em **Carregar viagem**.")
        return None

    current_trip_id = st.session_state.trip_id
    meta = db.fetch_trip_meta(conn, current_trip_id)
    if meta is None:
        st.error(f"Viagem {current_trip_id} não encontrada.")
        return None

    st.divider()
    _render_trip_info(meta)
    return meta


def main() -> None:
    """Render the two-column layout: selector/info on the left, map on the right."""
    conn = get_connection()
    left_col, right_col = st.columns([1, 2])

    with left_col, st.container(height=PANEL_HEIGHT_PX, border=True):
        meta = _render_selector_panel(conn)

    with right_col:
        if meta is None:
            fmap = maps.build_blank_map()
            map_key = "map_blank"
        else:
            trip_id = meta["trip_id"]
            stops = db.fetch_stops(conn, trip_id)
            positions = db.fetch_positions(conn, trip_id)
            shape = db.fetch_shape(conn, trip_id)
            fares = db.fetch_fares(conn, trip_id)
            fmap = maps.build_trip_map(meta, stops, positions, fares, shape)
            map_key = f"map_{trip_id}"

        st_folium(
            fmap,
            height=PANEL_HEIGHT_PX,
            use_container_width=True,
            returned_objects=[],
            key=map_key,
        )


main()
