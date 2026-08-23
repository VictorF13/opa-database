"""Pair-validation UI: confirm or reject a whole `(bus, device)` pairing.

Run with::

    uv run streamlit run ml/bus_matching_model/app/pair_labeler.py --server.port 8502

A different question from `streamlit_app.py`'s trip labeler, and a
different unit. That one asks "which candidate drove this *trip*" and
feeds the day model. This one asks "is this device this bus's device
for the month", which is the question the deliverable is actually about
-- and because a device essentially never changes bus mid-month, one
decision here settles ~20 days of evidence at once and implies a
negative for every rival candidate of that bus.

**Layout, on request**: candidates across as columns, sampled trips
down as rows, one map per cell -- so genuinely confusing candidates are
compared side by side on the same trips rather than judged one at a
time from memory.

**Queue**: the buses whose top two candidates are closest, excluding
buses with no real evidence for anything (see
`pair_model.select_hard_buses` -- without that filter the queue fills
with zero-evidence buses whose near-zero scores produce a near-zero
margin).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import db  # noqa: E402
import exclusions  # noqa: E402
import maps  # noqa: E402
import pair_features  # noqa: E402
import pair_model  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
import streamlit_folium  # noqa: E402
import training  # noqa: E402
from features import DAY_FEATURE_NAMES  # noqa: E402
from gtfs_cache import build_shape_cache  # noqa: E402
from schema import ensure_schema  # noqa: E402

if TYPE_CHECKING:
    import psycopg

FEATURES_V2_DIR = Path(__file__).resolve().parents[1] / "artifacts" / "features_v2"

MAX_CANDIDATE_COLUMNS = 4
MAX_TRIP_ROWS = 3
MAP_HEIGHT_PX = 240
RETRAIN_EVERY = 10

# Default ship threshold. Not a guess dressed up as a constant: it is
# the *starting* point for `pair_model.precision_at_threshold`, which
# reports measured precision at whatever cut is chosen, so this number
# is meant to be moved once there are labels to measure against.
DEFAULT_SHIP_THRESHOLD = 0.90

st.set_page_config(page_title="Bus-Device Pair Validation", layout="wide")


@st.cache_resource
def get_connection() -> psycopg.Connection:
    """Open (and cache) this app's database connection."""
    conn = db.get_connection()
    ensure_schema(conn)
    return conn


@st.cache_resource
def get_shape_cache(_conn: psycopg.Connection) -> dict[tuple, Any]:
    """Build (and cache) the GTFS shape cache."""
    return build_shape_cache(_conn)


@st.cache_data(show_spinner="Loading month features...")
def load_day_features() -> pd.DataFrame:
    """Every date's v2 feature parquet, concatenated."""
    paths = sorted(FEATURES_V2_DIR.glob("date=*.parquet"))
    if not paths:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)


@st.cache_data(show_spinner="Building pair features...")
def build_pairs(_conn: psycopg.Connection, n_labels: int) -> pd.DataFrame:  # noqa: ARG001
    """Day features -> day model -> month-level pair features.

    Args:
        _conn: An open connection (underscore: not a cache key).
        n_labels: Trip-label count, included purely so the cache
            invalidates when the day model's training data changes.

    Returns:
        `pair_features.build_pair_features` output, minus excluded buses.

    """
    day = load_day_features()
    if day.empty:
        return pd.DataFrame()

    excluded = exclusions.excluded_bus_ids(_conn)
    day = day[~day["bus_id"].isin(excluded)]

    expanded = db.expand_labels_to_training_rows(_conn, day, None)
    if expanded.empty or expanded["label"].nunique() < pair_model.BOTH_CLASSES:
        return pd.DataFrame()

    day_model = training.train_model(
        expanded[DAY_FEATURE_NAMES], expanded["label"].to_numpy()
    )
    day_scores = training.predict_positive_proba(day_model, day[DAY_FEATURE_NAMES])
    return pair_features.build_pair_features(_conn, day, pd.Series(day_scores))


def _fit_pair_model(
    conn: psycopg.Connection, pairs: pd.DataFrame, labels: pd.DataFrame
) -> dict[str, Any] | None:
    """Fit the pair model and persist it, so a restart doesn't lose it."""
    rows = pair_model.build_training_rows(pairs, labels)
    state = pair_model.train_pair_model(rows)
    if state is not None:
        state["run_id"] = pair_model.save_pair_run(
            conn, state, n_labeled_pairs=int(labels["bus_id"].nunique())
        )
        conn.commit()
    return state


def _sample_trips_for_bus(conn: psycopg.Connection, bus_id: str) -> pd.DataFrame:
    """Pick a few of this bus's trips, spread across different dates.

    Spread matters: two trips from the same morning would show the same
    corridor twice, while trips from different dates test whether a
    candidate tracks the bus consistently over the month -- which is the
    actual claim being judged.
    """
    trips = pd.read_sql(
        """
        SELECT trip_id, bus_id, route_id, trip_date,
               trip_start_timestamp, trip_end_timestamp,
               gtfs_feed_version_date, gtfs_shape_id_i, gtfs_shape_id_v
        FROM ml.trip_validity_final
        WHERE is_valid AND bus_id = %(bus_id)s
        ORDER BY trip_start_timestamp;
        """,
        conn,
        params={"bus_id": bus_id},
    )
    if trips.empty:
        return trips
    per_date = trips.groupby("trip_date", group_keys=False).head(1)
    step = max(len(per_date) // MAX_TRIP_ROWS, 1)
    return per_date.iloc[::step].head(MAX_TRIP_ROWS).reset_index(drop=True)


def _shapes_for_trip(conn: psycopg.Connection, trip: pd.Series) -> dict[str, tuple]:
    shapes: dict[str, tuple] = {}
    for direction, col in (("I", "gtfs_shape_id_i"), ("V", "gtfs_shape_id_v")):
        shape_id = trip[col]
        if shape_id is None or pd.isna(shape_id):
            continue
        geojson = db.fetch_shape_geojson(conn, trip["gtfs_feed_version_date"], shape_id)
        stops = db.fetch_route_stops(conn, trip["gtfs_feed_version_date"], shape_id)
        if geojson:
            shapes[direction] = (geojson, stops)
    return shapes


def _on_mode_change() -> None:
    """Drop the current bus so the new mode picks its own next one."""
    st.session_state.current_bus = None


_STOPPING_RENDER = {
    "too_early": st.info,
    "keep_going": st.info,
    "precision_low": st.warning,
    "queue_empty": st.warning,
    "done": st.success,
}


def _render_progress(
    ranked: pd.DataFrame, labels: pd.DataFrame, threshold: float
) -> None:
    prog = pair_model.progress_summary(ranked, labels, threshold)
    cols = st.columns(5)
    cols[0].metric("Buses in scope", prog["total_buses"])
    cols[1].metric("Done (confirmed or confident)", prog["done"])
    cols[2].metric("Confirmed by hand", prog["confirmed_by_hand"])
    cols[3].metric("Model-confident", prog["model_confident"])
    cols[4].metric("Remaining", prog["remaining"])
    st.progress(
        prog["done"] / prog["total_buses"] if prog["total_buses"] else 0.0,
        text=f"{prog['done']} / {prog['total_buses']} buses settled",
    )

    audit = pair_model.audit_precision(labels)
    if audit["n"]:
        st.caption(
            f"**Unbiased precision (audit sample): {audit['precision']:.1%}** "
            f"({audit['n_correct']}/{audit['n']} randomly-sampled confident picks "
            "correct). This is the number to trust."
        )
        bands = audit.get("by_band") or []
        if bands:
            st.caption(
                "By confidence band — an overall figure hides which band the "
                "labels came from, and the bands are what differ: "
                + " · ".join(
                    f"**{b['band']}**: {b['precision']:.0%} ({b['n_correct']}/{b['n']})"
                    for b in bands
                )
            )
    else:
        st.caption(
            "**No audit labels yet, so there is no unbiased precision figure.** "
            "Queue labels alone cannot reveal a confidently-wrong prediction, "
            "because the queue only ever shows ambiguous buses. Switch to "
            "'Audit confident picks' in the sidebar to start measuring."
        )

    prec = pair_model.precision_at_threshold(ranked, labels, threshold)
    if prec["n"]:
        st.caption(
            f"Precision over *all* labels at {threshold:.2f}: "
            f"{prec['precision']:.1%} ({prec['n_correct']}/{prec['n']}) -- "
            "includes queue labels, which are drawn from the hardest cases, so "
            "read it as a floor rather than an estimate."
        )

    signal = pair_model.stopping_signal(ranked, labels, threshold)
    render = _STOPPING_RENDER.get(signal["verdict"], st.info)
    render(f"**When to stop:** {signal['message']}")


def _record(
    conn: psycopg.Connection,
    *,
    bus_id: str,
    device_id: str,
    verdict: str,
    is_top: bool,
    confidence: float | None,
    n_candidates: int,
) -> None:
    pair_model.insert_pair_label(
        conn,
        bus_id=bus_id,
        device_id=device_id,
        verdict=verdict,
        was_top_candidate=is_top,
        model_confidence=confidence,
        n_candidates=n_candidates,
        label_source=st.session_state.get("label_mode", "queue"),
    )
    conn.commit()
    st.session_state.labels_since_fit += 1
    st.session_state.current_bus = None
    st.rerun()


def _render_sidebar(ranked: pd.DataFrame, state: dict[str, Any] | None) -> float:
    """Draw the sidebar and return the chosen ship threshold."""
    with st.sidebar:
        st.subheader("What to label")
        st.radio(
            "Sampling mode",
            options=["queue", "audit"],
            format_func=lambda m: (
                "Hard cases — teach the model"
                if m == "queue"
                else "Audit confident picks — measure it"
            ),
            key="label_mode",
            on_change=_on_mode_change,
            help=(
                "Hard cases are the most ambiguous buses: best for teaching, "
                "but their precision understates the system because they are "
                "deliberately the difficult ones. Audit randomly samples buses "
                "the model is ALREADY confident about -- the only way to catch "
                "a confidently-wrong prediction, and the only unbiased "
                "precision estimate."
            ),
        )
        st.divider()
        st.subheader("Ship threshold")
        threshold = st.slider(
            "Confidence to count a bus as settled",
            min_value=0.50,
            max_value=0.99,
            value=DEFAULT_SHIP_THRESHOLD,
            step=0.01,
            help=(
                "Not a hardcoded constant -- pick it by watching measured "
                "precision in the main panel."
            ),
        )
        st.divider()
        if state is None:
            st.info(
                f"Pair model not fitted yet (needs ~{pair_model.MIN_PAIRS_TO_TRAIN} "
                "labeled pairs). Ordering falls back to the day model's own score, "
                "so labeling works from the first click."
            )
        else:
            m = state["metrics"]
            st.success("Pair model fitted")
            st.caption(
                f"train {state['n_train']} / test {state['n_test']} rows · "
                f"{len(state['selected_features'])} features"
            )
            st.caption(
                f"holdout AUC {m['auc']:.3f} · Brier {m['brier']:.3f} · "
                f"ECE {m['ece']:.3f}"
            )
            cv = state.get("cv", {})
            if cv.get("n_folds"):
                st.caption(
                    f"{cv['n_folds']}-fold CV AUC "
                    f"{cv['auc_mean']:.3f} ± {cv['auc_std']:.3f} · "
                    f"ECE {cv['ece_mean']:.3f} — the steadier read"
                )
            if state.get("run_id"):
                st.caption(f"saved as pair run #{state['run_id']}")
        st.divider()
        no_ev = pair_model.no_evidence_buses(ranked)
        st.caption(
            f"{len(no_ev)} buses have no real evidence for any candidate "
            "(excluded from the queue -- they need candidate generation, not a "
            "human decision)."
        )
    return threshold


def _render_candidate_headers(bus_pairs: pd.DataFrame) -> None:
    """One column header per candidate: confidence and supporting counts."""
    header_cols = st.columns(len(bus_pairs))
    for col, (_, cand) in zip(header_cols, bus_pairs.iterrows(), strict=False):
        with col:
            badge = " · dictionary" if cand["n_dictionary_sources"] > 0 else ""
            st.markdown(f"**{cand['device_id']}**{badge}")
            st.metric("Confidence", f"{cand['pair_score']:.3f}")
            st.caption(
                f"{int(cand['n_days_with_data'])} days with data · "
                f"day-score mean {cand['day_score_mean']:.3f}"
            )


def _render_trip_grid(
    conn: psycopg.Connection,
    bus_id: str,
    trips: pd.DataFrame,
    bus_pairs: pd.DataFrame,
) -> None:
    """Draw the comparison grid: one row per trip, one map per candidate."""
    for _, trip in trips.iterrows():
        st.markdown(
            f"**{trip['trip_date']}** · route {trip['route_id']} · "
            f"{trip['trip_start_timestamp']:%H:%M}-{trip['trip_end_timestamp']:%H:%M}"
        )
        shapes = _shapes_for_trip(conn, trip)
        row_cols = st.columns(len(bus_pairs))
        for col, (_, cand) in zip(row_cols, bus_pairs.iterrows(), strict=False):
            with col:
                track = db.fetch_device_trip_positions(
                    conn,
                    cand["device_id"],
                    trip["trip_start_timestamp"],
                    trip["trip_end_timestamp"],
                )
                if not shapes:
                    st.caption("no GTFS shape")
                elif track.empty:
                    st.caption("no AVL in this window")
                else:
                    streamlit_folium.st_folium(
                        maps.build_candidate_map(shapes, track),
                        height=MAP_HEIGHT_PX,
                        use_container_width=True,
                        returned_objects=[],
                        key=f"m_{bus_id}_{trip['trip_id']}_{cand['device_id']}",
                    )
        st.divider()


def _ensure_pair_state(
    conn: psycopg.Connection, pairs: pd.DataFrame, labels: pd.DataFrame
) -> dict | None:
    """Fit the pair model on first load and every `RETRAIN_EVERY` labels."""
    needs_fit = (
        "pair_state" not in st.session_state
        or st.session_state.labels_since_fit >= RETRAIN_EVERY
    )
    if needs_fit:
        with st.spinner("Fitting pair model..."):
            st.session_state.pair_state = _fit_pair_model(conn, pairs, labels)
        st.session_state.labels_since_fit = 0
    return st.session_state.pair_state


def main() -> None:
    """Render the pair-validation UI."""
    conn = get_connection()
    if "labels_since_fit" not in st.session_state:
        st.session_state.labels_since_fit = 0
    if "current_bus" not in st.session_state:
        st.session_state.current_bus = None

    trip_label_count = db.trip_label_counts(conn)["total"]
    pairs = build_pairs(conn, trip_label_count)
    if pairs.empty:
        st.error(
            "No pair features yet -- run scripts/build_features.py and make sure "
            "some trip labels exist."
        )
        return

    labels = pair_model.fetch_pair_labels(conn)
    state = _ensure_pair_state(conn, pairs, labels)
    ranked = pair_model.rank_pairs(pairs, state)
    threshold = _render_sidebar(ranked, state)

    st.title("Bus-Device Pair Validation")
    _render_progress(ranked, labels, threshold)
    st.divider()

    mode = st.session_state.get("label_mode", "queue")
    if mode == "audit":
        queue = pair_model.select_audit_buses(ranked, labels, threshold, limit=50)
        empty_msg = f"No unlabeled buses above {threshold:.2f} left to audit."
    else:
        queue = pair_model.select_hard_buses(ranked, labels, limit=50)
        empty_msg = "Nothing ambiguous left in the queue."
    if queue.empty:
        st.success(empty_msg)
        return

    if st.session_state.current_bus is None:
        st.session_state.current_bus = queue.iloc[0]["bus_id"]
    bus_id = st.session_state.current_bus

    bus_pairs = ranked[ranked["bus_id"] == bus_id].nlargest(
        MAX_CANDIDATE_COLUMNS, "pair_score"
    )
    n_candidates = int(bus_pairs["n_candidates_for_bus"].iloc[0])
    margin = float(bus_pairs["pair_margin"].iloc[0])

    st.subheader(f"Bus {bus_id}")
    st.caption(
        f"{n_candidates} candidates · top-two margin {margin:.3f} "
        f"· showing the {len(bus_pairs)} best"
    )

    trips = _sample_trips_for_bus(conn, bus_id)
    if trips.empty:
        st.warning("No valid trips for this bus.")
        return

    _render_candidate_headers(bus_pairs)
    _render_trip_grid(conn, bus_id, trips, bus_pairs)
    _render_verdicts(conn, bus_id, bus_pairs, queue, n_candidates)


def _render_verdicts(
    conn: psycopg.Connection,
    bus_id: str,
    bus_pairs: pd.DataFrame,
    queue: pd.DataFrame,
    n_candidates: int,
) -> None:
    """Draw the decision row: pick a candidate, reject the top pick, or defer."""
    st.subheader("Verdict")
    verdict_cols = st.columns(len(bus_pairs))
    for col, (_, cand) in zip(verdict_cols, bus_pairs.iterrows(), strict=False):
        with col:
            if st.button(
                f"✓ {cand['device_id']} is correct",
                key=f"ok_{cand['device_id']}",
                width="stretch",
                type="primary" if cand["pair_rank"] == 1 else "secondary",
            ):
                _record(
                    conn,
                    bus_id=bus_id,
                    device_id=cand["device_id"],
                    verdict="correct",
                    is_top=bool(cand["pair_rank"] == 1),
                    confidence=float(cand["pair_score"]),
                    n_candidates=n_candidates,
                )

    top = bus_pairs.iloc[0]
    action_cols = st.columns(3)
    if action_cols[0].button("✗ Top pick is wrong", width="stretch"):
        _record(
            conn,
            bus_id=bus_id,
            device_id=top["device_id"],
            verdict="wrong",
            is_top=True,
            confidence=float(top["pair_score"]),
            n_candidates=n_candidates,
        )
    if action_cols[1].button("? Unsure", width="stretch"):
        _record(
            conn,
            bus_id=bus_id,
            device_id=top["device_id"],
            verdict="unsure",
            is_top=True,
            confidence=float(top["pair_score"]),
            n_candidates=n_candidates,
        )
    if action_cols[2].button("Skip to another bus", width="stretch"):
        remaining = queue[queue["bus_id"] != bus_id]
        st.session_state.current_bus = (
            remaining.iloc[0]["bus_id"] if not remaining.empty else None
        )
        st.rerun()


main()
