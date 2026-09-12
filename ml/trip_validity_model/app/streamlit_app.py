"""Streamlit active-learning labeling UI for the Trip Validity model.

Run with `uv run streamlit run ml/trip_validity_model/app/streamlit_app.py`
from the repo root.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from typing import TYPE_CHECKING, Any

import db
import maps
import metrics
import pandas as pd
import registry
import sampling
import streamlit as st
import training
from calibration import PlattCalibrator
from features import ALL_FEATURES, cast_feature_dtypes
from schema import ensure_schema
from streamlit_folium import st_folium

if TYPE_CHECKING:
    import datetime

    import psycopg

FEATURE_PANEL_DEFAULTS = [
    "trip_duration_ratio_to_route_avg",
    "trip_fare_count",
    "fare_span_ratio_to_duration",
    "trip_distance_ratio_to_route_i",
    "trip_distance_ratio_to_route_v",
    "path_match_score_frechet_i",
    "path_match_score_frechet_v",
]

# Folium/Leaflet maps render into an iframe, which (like every other
# HTML embed) has no way to size itself to "however much vertical space
# happens to be free" in pure Python/Streamlit - some pixel height has
# to be given explicitly. Width has no such limitation, so the map
# itself is rendered with use_container_width=True instead of a second
# hardcoded number.
MAP_HEIGHT_PX = 320
_FORTALEZA_TZ = timezone(timedelta(hours=-3))

st.set_page_config(page_title="Trip Validity Labeler", layout="wide")


def _labeled_value(label: str, value: object) -> None:
    st.caption(label)
    st.markdown(f"**{value}**")


def _format_trip_window(opening: datetime.datetime, closing: datetime.datetime) -> str:
    start = opening.astimezone(_FORTALEZA_TZ)
    end = closing.astimezone(_FORTALEZA_TZ)
    end_str = (
        end.strftime("%H:%M:%S")
        if start.date() == end.date()
        else end.strftime("%Y-%m-%d %H:%M:%S")
    )
    return f"{start.strftime('%Y-%m-%d %H:%M:%S')} - {end_str}"


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "N/A"
    return str(timedelta(seconds=int(seconds)))


@st.cache_resource
def get_connection() -> psycopg.Connection:
    """Open (and cache across reruns) the app's single database connection.

    Returns:
        An open, autocommit connection with the app's own tables ensured.

    """
    conn = db.get_connection()
    ensure_schema(conn)
    return conn


def _predict(model_state: dict[str, Any], row: dict[str, Any]) -> float:
    selected_features = model_state["config"].selected_features
    features = cast_feature_dtypes(pd.DataFrame([row]))[selected_features]
    raw = training.predict_positive_proba(model_state["model"], features)
    return float(model_state["calibrator"].predict(raw)[0])


def _load_latest_model(conn: psycopg.Connection) -> dict[str, Any] | None:
    run_row = db.fetch_latest_model_run(conn)
    if run_row is None:
        return None
    loaded = registry.load_run(run_row)
    loaded["run_row"] = run_row
    return loaded


def _run_training_cycle(
    conn: psycopg.Connection, run_type: str, n_train_labels: int
) -> None:
    train = cast_feature_dtypes(db.fetch_label_set(conn, "train"))
    calibration = cast_feature_dtypes(db.fetch_label_set(conn, "calibration"))
    test = cast_feature_dtypes(db.fetch_label_set(conn, "test"))

    train_features, train_labels = train[ALL_FEATURES], train["label"].to_numpy()

    cv_brier = None
    if run_type == "milestone":
        result = training.run_milestone(train_features, train_labels)
        config = result.config
        cv_brier = result.cv_brier_score
    else:
        config = st.session_state.model_state["config"]

    model = training.train_final_model(train_features, train_labels, config)

    raw_calibration = training.predict_positive_proba(
        model, calibration[config.selected_features]
    )
    calibrator = PlattCalibrator().fit(raw_calibration, calibration["label"].to_numpy())

    raw_test = training.predict_positive_proba(model, test[config.selected_features])
    calibrated_test = calibrator.predict(raw_test)
    test_metrics = metrics.evaluate(test["label"].to_numpy(), calibrated_test)

    full_pool = cast_feature_dtypes(db.fetch_unlabeled_pool(conn, labelable_only=False))
    raw_full_pool = training.predict_positive_proba(
        model, full_pool[config.selected_features]
    )
    confident_count, confident_total = metrics.confident_prediction_count(
        calibrator.predict(raw_full_pool)
    )

    registry.save_run(
        conn,
        run_type=run_type,
        n_train_labels=n_train_labels,
        model=model,
        calibrator=calibrator,
        config=config,
        cv_brier_score=cv_brier,
        test_metrics=test_metrics,
        confident_90_count=confident_count,
        confident_90_total=confident_total,
    )

    st.session_state.model_state = {
        "model": model,
        "calibrator": calibrator,
        "config": config,
    }
    _rebuild_uncertain_queue(conn)


def _rebuild_uncertain_queue(conn: psycopg.Connection) -> None:
    """Refresh the cached most-uncertain trip_ids from the current model.

    Called after every retrain, and lazily by `_ensure_candidate` whenever
    the cache has run dry between retrains. The cache only ever lives in
    `st.session_state`, not the database, so it starts empty again after
    every app restart; without this, draws would silently fall back to
    pure random until the next scheduled retrain instead of keeping the
    usual uncertain/random mix.

    Args:
        conn: An open connection.

    """
    model_state = st.session_state.model_state
    if model_state is None:
        st.session_state.uncertain_queue = []
        return
    labelable_pool = cast_feature_dtypes(
        db.fetch_unlabeled_pool(
            conn,
            labelable_only=True,
            exclude_trip_ids=st.session_state.skipped_trip_ids,
        )
    )
    if labelable_pool.empty:
        st.session_state.uncertain_queue = []
        return
    raw = training.predict_positive_proba(
        model_state["model"], labelable_pool[model_state["config"].selected_features]
    )
    calibrated = model_state["calibrator"].predict(raw)
    st.session_state.uncertain_queue = sampling.build_uncertain_queue(
        labelable_pool, calibrated
    )


def _ensure_candidate(conn: psycopg.Connection) -> None:
    if st.session_state.get("candidate") is not None:
        return
    if (
        not st.session_state.uncertain_queue
        and st.session_state.model_state is not None
    ):
        with st.spinner("Refreshing uncertainty ranking..."):
            _rebuild_uncertain_queue(conn)
    st.session_state.candidate = sampling.draw_candidate(
        conn, st.session_state.uncertain_queue, st.session_state.skipped_trip_ids
    )


def _feature_panel(row: dict[str, Any], model_state: dict[str, Any] | None) -> None:
    top_features = (
        model_state["config"].selected_features[:7]
        if model_state is not None
        else FEATURE_PANEL_DEFAULTS
    )
    st.caption(
        "Top model features" if model_state is not None else "Default feature preview"
    )
    for name in top_features:
        value = row.get(name)
        display = f"{value:.3f}" if isinstance(value, float) else str(value)
        _labeled_value(name, display)


def _render_metrics_history(conn: psycopg.Connection) -> None:
    runs = db.fetch_model_runs(conn)
    if runs.empty:
        st.info(
            "No trained model yet. Metrics appear once the 50-row seed "
            "training pool is complete."
        )
        return
    st.subheader("Model metrics over time")
    st.line_chart(
        runs.set_index("n_train_labels")[["test_brier", "test_auc", "test_ece"]]
    )

    milestones = runs[runs["run_type"] == "milestone"]
    if not milestones.empty:
        st.caption("Milestone (real) metrics")
        st.dataframe(
            milestones[
                [
                    "n_train_labels",
                    "cv_brier_score",
                    "test_auc",
                    "test_brier",
                    "test_log_loss",
                    "test_ece",
                    "confident_90_count",
                    "confident_90_total",
                ]
            ],
            hide_index=True,
        )

    latest = runs.iloc[-1]
    if latest["confident_90_total"]:
        pct = 100 * latest["confident_90_count"] / latest["confident_90_total"]
        st.metric("Predictions >=90% confident (latest run)", f"{pct:.1f}%")


def _render_maps(trip_id: int, map_data: dict[str, Any], n_positions: int) -> None:
    has_i, has_v = bool(map_data["shape_i"]), bool(map_data["shape_v"])
    if has_i and has_v:
        map_cols = st.columns(2)
    elif has_i or has_v:
        map_cols = st.columns(1)
    else:
        st.warning("No matched GTFS shape for this trip's route.")
        return

    # Keyed on trip_id so Streamlit fully remounts the map component per
    # trip instead of reusing the previous one's browser-side state
    # (view position, etc.), which is what made the map look "stuck".
    next_col = iter(map_cols)
    if has_i:
        with next(next_col):
            st.caption(f"Direction I · {n_positions} AVL positions plotted")
            st_folium(
                maps.build_direction_map(map_data["shape_i"], map_data["positions"]),
                height=MAP_HEIGHT_PX,
                use_container_width=True,
                returned_objects=[],
                key=f"map_{trip_id}_i",
            )
    if has_v:
        with next(next_col):
            st.caption(f"Direction V · {n_positions} AVL positions plotted")
            st_folium(
                maps.build_direction_map(map_data["shape_v"], map_data["positions"]),
                height=MAP_HEIGHT_PX,
                use_container_width=True,
                returned_objects=[],
                key=f"map_{trip_id}_v",
            )


def _render_trip_info(
    row: dict[str, Any], n_positions: int, predicted_probability: float | None
) -> None:
    _labeled_value("Bus", row["bus_id"])
    _labeled_value("Route", row["route_id"])
    _labeled_value(
        "Trip window",
        _format_trip_window(
            row["trip_opening_timestamp"], row["trip_closing_timestamp"]
        ),
    )
    _labeled_value("Duration", _format_duration(row["trip_duration_seconds"]))
    _labeled_value("Fares", row["trip_fare_count"])
    _labeled_value("AVL positions", n_positions)
    if predicted_probability is not None:
        _labeled_value("Calibrated P(valid)", f"{predicted_probability:.3f}")


def _handle_decision(
    conn: psycopg.Connection,
    candidate: sampling.Candidate,
    *,
    label: bool,
    predicted_probability: float | None,
) -> None:
    db.insert_label(
        conn,
        trip_id=candidate.trip_id,
        label=label,
        label_set=candidate.label_set,
        selection_source=candidate.selection_source,
        predicted_probability=predicted_probability,
    )
    st.session_state.candidate = None
    if candidate.label_set == "train":
        counts = db.label_set_counts(conn)
        n_train_labels = counts["train"]
        run_type = sampling.landmark_crossed(n_train_labels, counts)
        if run_type is not None:
            with st.spinner(f"Running {run_type} retrain..."):
                _run_training_cycle(conn, run_type, n_train_labels)
    st.rerun()


def _render_decision_buttons(
    conn: psycopg.Connection,
    candidate: sampling.Candidate,
    predicted_probability: float | None,
) -> None:
    valid_col, invalid_col, skip_col = st.columns(3)
    if valid_col.button("Valid trip", type="primary", width="stretch"):
        _handle_decision(
            conn, candidate, label=True, predicted_probability=predicted_probability
        )
    if invalid_col.button("Invalid trip", width="stretch"):
        _handle_decision(
            conn, candidate, label=False, predicted_probability=predicted_probability
        )
    if skip_col.button("Skip", width="stretch"):
        st.session_state.skipped_trip_ids.add(candidate.trip_id)
        st.session_state.candidate = None
        st.rerun()


def main() -> None:
    """Render the labeling UI and drive the active learning loop."""
    conn = get_connection()
    if "model_state" not in st.session_state:
        st.session_state.model_state = _load_latest_model(conn)
    if "uncertain_queue" not in st.session_state:
        st.session_state.uncertain_queue = []
    if "skipped_trip_ids" not in st.session_state:
        # Session-only: never written to the database (a skip means
        # "pretend I never saw it"), so this resets on every app restart.
        st.session_state.skipped_trip_ids = set()

    st.subheader("Trip Validity — Active Learning Labeler")

    counts = db.label_set_counts(conn)
    phase = sampling.current_phase(counts)
    total_budget = sampling.TRAIN_CAP + sampling.CALIBRATION_CAP + sampling.TEST_CAP
    total_labeled = counts["train"] + counts["calibration"] + counts["test"]
    st.caption(
        f"**{total_labeled}/{total_budget} labeled** "
        f"({total_budget - total_labeled} to go) · "
        f"Calibration {counts['calibration']}/{sampling.CALIBRATION_CAP} · "
        f"Test {counts['test']}/{sampling.TEST_CAP} · "
        f"Train {counts['train']}/{sampling.TRAIN_CAP} · Phase: {phase}"
    )
    st.progress(min(total_labeled / total_budget, 1.0))

    _ensure_candidate(conn)
    candidate: sampling.Candidate | None = st.session_state.candidate
    if candidate is None:
        st.success("Nothing left to label.")
        return

    row = db.fetch_trip_row(conn, candidate.trip_id)
    if row is None:
        st.session_state.candidate = None
        st.rerun()
        return
    map_data = db.fetch_trip_map_data(conn, candidate.trip_id)
    n_positions = len(map_data["positions"])

    predicted_probability: float | None = None
    if st.session_state.model_state is not None:
        predicted_probability = _predict(st.session_state.model_state, row)

    _render_decision_buttons(conn, candidate, predicted_probability)
    _render_maps(candidate.trip_id, map_data, n_positions)

    with st.expander("Model metrics over time"):
        _render_metrics_history(conn)

    with st.sidebar:
        st.subheader("Trip info")
        _render_trip_info(row, n_positions, predicted_probability)
        st.divider()
        st.subheader("Features")
        _feature_panel(row, st.session_state.model_state)


main()
