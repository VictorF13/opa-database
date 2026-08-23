"""Streamlit active-learning labeling UI for the Bus Matching model.

Run with `uv run streamlit run ml/bus_matching_model/app/streamlit_app.py`
from the repo root.

**Labeling unit is one trip, not one bus-date.** Each decision judges
only the trip on screen ("which candidate looks right for THIS trip"),
recorded in `ml.bus_matching_trip_labels`. A bus-date's day-level
verdict is a continuous probabilistic belief (`belief.py`), not a hard
vote count: each vote is noisy evidence that shifts belief, starting
from a prior seeded by the Tier 1 cold-start score. A bus-date only
counts as "resolved" (and feeds training) once its top option clears a
confidence bar *and* is clearly separated from the runner-up -- so a
single mistaken click can never resolve anything by itself, and
disagreeing votes just leave it open rather than forcing a verdict.
Confirming a device on one bus-date also discounts that same device's
belief on every *other* bus-date that lists it as a candidate the same
day (`belief.py`'s cross-suppression), which is what lets labeling one
bus confidently help disambiguate others without a separate vote.

**No manual queue.** `db.fetch_next_trip` always picks the single most
informative next trip on its own -- the unresolved bus-date with the
smallest belief margin (closest call) that still has an unlabeled
sample trip. Decision controls live in the sidebar, which Streamlit
keeps fixed in place while the main panel (maps) scrolls, so they stay
clickable without hunting for them.

**On request**: score + dictionary-origin badge shown directly on every
candidate (both sidebar and main panel), candidates ordered best to
worst score -- the plan's anti-bias hiding, deliberately overridden.
The chainage-time plot was tried and dropped (not useful in practice);
maps are the only evidence shown now, two per candidate (one per GTFS
direction).

**Scope of this first pass**, relative to the full plan (Section 4):
implemented -- contested/uncontested routing off
`ml.bus_matching_contestedness`, two maps per candidate device (route +
stops + that candidate's own white-to-black AVL trail), forced-choice-
in-contested / yes-no-unsure-in-uncontested decisions, model-informed
belief once a model exists (Section 5). **Not yet implemented**: the
full batch `linear_sum_assignment` per date (Section 9) and temporal
smoothing (Section 10) -- those stay periodic background jobs, not part
of this live UI -- the day-summary strip, and real keyboard shortcuts
(buttons only, matching how `trip_validity_model`'s own labeler is
actually built despite the plan mentioning keys).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import db  # noqa: E402
import maps  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import registry  # noqa: E402
import streamlit as st  # noqa: E402
import streamlit_folium  # noqa: E402
import training  # noqa: E402
from calibration import PlattCalibrator  # noqa: E402
from features import DAY_FEATURE_NAMES, select_sample_trips  # noqa: E402
from gtfs_cache import build_shape_cache  # noqa: E402
from metrics import evaluate as evaluate_metrics  # noqa: E402
from schema import ensure_schema  # noqa: E402

if TYPE_CHECKING:
    import datetime
    from collections.abc import Callable

    import psycopg

MAP_HEIGHT_PX = 280

# The plan expects a total label budget in the low hundreds (Section
# 11), nowhere near trip_validity_model's 500-row calibration/test
# budget -- so the first retrain happens much sooner, and every retrain
# afterwards is a full refit (no incremental "cycle" vs "milestone"
# split; that distinction is deferred to the one proper tuning pass
# after labeling converges, per Section 5). Thresholds count *resolved
# bus-dates* (db.resolved_bus_dates), not raw trip clicks, since that's
# what actually produces training rows.
SEED_LABEL_THRESHOLD = 15
RETRAIN_INTERVAL = 5
MIN_CLASS_COUNT = 3
MIN_BUS_DATES_FOR_SPLIT = 5
MIN_LABEL_CLASSES = 2

st.set_page_config(page_title="Bus Matching Labeler", layout="wide")


@st.cache_resource
def get_connection() -> psycopg.Connection:
    """Open (and cache across reruns) the app's single database connection."""
    conn = db.get_connection()
    ensure_schema(conn)
    return conn


@st.cache_resource
def get_shape_cache(_conn: psycopg.Connection) -> dict[tuple, Any]:
    """Build (and cache across reruns) the GTFS shape cache."""
    return build_shape_cache(_conn)


@st.cache_data(ttl=30)
def get_features() -> pd.DataFrame:
    """Load whatever Tier 1 feature parquet checkpoints exist so far.

    Cached for 30s at a time (not forever) since the full-month
    background build can still be writing new `date=*.parquet` files
    while this app is in use.
    """
    return db.load_available_features()


@st.cache_data(ttl=300)
def get_total_bus_dates(_conn: psycopg.Connection) -> int:
    """Total bus-dates in scope -- the resolution-progress denominator."""
    return db.total_bus_date_count(_conn)


def _available_shapes(row: pd.Series, shape_cache: dict[tuple, Any]) -> dict[str, Any]:
    """Every GTFS direction that actually has a cached shape for this trip."""
    shapes = {}
    for direction, shape_id_col in (("I", "gtfs_shape_id_i"), ("V", "gtfs_shape_id_v")):
        key = (row["gtfs_feed_version_date"], row[shape_id_col])
        if key in shape_cache:
            shapes[direction] = shape_cache[key]
    return shapes


def _ensure_current_trip(conn: psycopg.Connection, features: pd.DataFrame) -> None:
    if st.session_state.get("current") is not None:
        return
    next_trip = db.fetch_next_trip(
        conn,
        features,
        model_state=st.session_state.model_state,
        selection_mode=st.session_state.selection_mode,
        exclude_trip_ids=st.session_state.skipped_trips,
        exclude_bus_dates=st.session_state.skipped_bus_dates,
    )
    st.session_state.current = next_trip


def _on_selection_mode_change() -> None:
    """Force a fresh pick under the new mode instead of finishing out the old one."""
    st.session_state.current = None


def _load_latest_model(conn: psycopg.Connection) -> dict[str, Any] | None:
    run_row = db.fetch_latest_model_run(conn)
    if run_row is None:
        return None
    loaded = registry.load_run(run_row)
    loaded["run_row"] = run_row
    return loaded


def _group_split(
    bus_dates: list[tuple[str, Any]],
    *,
    test_frac: float = 0.3,
) -> tuple[set, set]:
    """Split `(bus_id, date)` groups into train/test.

    Splitting by bus-date (not by expanded row) keeps a bus-date's
    positive and negative rows -- which share almost all their context
    and are highly correlated -- on the same side of the split, rather
    than leaking related rows across train and test.

    No separate calibration split (on request: calibration is disabled,
    see `_run_training_cycle`) -- that slice is folded into `test`
    instead, so `test_ece` (the sole number `belief.model_prior_weight_for`
    now trusts) gets measured on more data than the old 15%-calib +
    15%-test split gave it.
    """
    rng = np.random.default_rng(42)
    shuffled = list(bus_dates)
    rng.shuffle(shuffled)
    n = len(shuffled)
    n_test = max(1, int(n * test_frac))
    test = set(shuffled[:n_test])
    train = set(shuffled[n_test:])
    return train, test


def _run_training_cycle(conn: psycopg.Connection) -> None:
    features = db.load_available_features()
    expanded = db.expand_labels_to_training_rows(
        conn, features, st.session_state.model_state
    )
    if expanded.empty:
        return
    class_counts = expanded["label"].value_counts()
    if (
        class_counts.get(True, 0) < MIN_CLASS_COUNT
        or class_counts.get(False, 0) < MIN_CLASS_COUNT
    ):
        return

    bus_dates = list(
        expanded[["bus_id", "date"]]
        .drop_duplicates()
        .itertuples(index=False, name=None)
    )
    if len(bus_dates) < MIN_BUS_DATES_FOR_SPLIT:
        return
    train_keys, test_keys = _group_split(bus_dates)

    def _subset(keys: set) -> pd.DataFrame:
        mask = expanded.apply(lambda r: (r["bus_id"], r["date"]) in keys, axis=1)
        return expanded[mask]

    train_df, test_df = _subset(train_keys), _subset(test_keys)
    if train_df["label"].nunique() < MIN_LABEL_CLASSES:
        return

    model = training.train_model(
        train_df[DAY_FEATURE_NAMES], train_df["label"].to_numpy()
    )

    # Calibration disabled, on request: a Platt fit on a bus-date-count
    # this small kept swinging wildly retrain to retrain (intercept
    # ranged -1.8 to +0.66 across 18 real retrains) even after the old
    # `MIN_CALIB_POSITIVES` guard, and that swing alone moved live
    # coverage by ~9x with no change in the underlying model -- confirmed
    # live by rescoring the same model through two different fitted
    # calibrators. Identity mapping means `score` downstream is the raw
    # model probability, so `test_ece` below measures the *model's own*
    # calibration quality directly, and `belief.model_prior_weight_for`
    # already refuses to trust it until that's genuinely good -- nothing
    # else needs to change to revisit real calibration later, once there
    # are enough resolved bus-dates for a calibration split to be stable
    # in its own right.
    calibrator = PlattCalibrator.from_params(coefficient=1.0, intercept=0.0)

    test_metrics = {
        "auc": float("nan"),
        "brier": float("nan"),
        "log_loss": float("nan"),
        "ece": float("nan"),
    }
    if not test_df.empty:
        raw_test = training.predict_positive_proba(model, test_df[DAY_FEATURE_NAMES])
        calibrated_test = calibrator.predict(raw_test)
        test_metrics = evaluate_metrics(test_df["label"].to_numpy(), calibrated_test)

    registry.save_run(
        conn,
        n_train_labels=len(train_df),
        model=model,
        calibrator=calibrator,
        selected_features=DAY_FEATURE_NAMES,
        hyperparameters=training.FIXED_HYPERPARAMETERS,
        test_metrics=test_metrics,
        n_resolved_bus_dates=len(bus_dates),
        n_trip_labels_total=db.trip_label_counts(conn)["total"],
    )
    # Re-fetch rather than construct the row by hand, so `run_row` (used
    # by `belief.model_prior_weight_for` to ramp trust) is populated
    # immediately -- without this, a freshly retrained model would fall
    # back to zero prior weight until the next full page reload.
    st.session_state.model_state = {
        "model": model,
        "calibrator": calibrator,
        "selected_features": DAY_FEATURE_NAMES,
        "run_row": db.fetch_latest_model_run(conn),
    }


def _maybe_retrain(conn: psycopg.Connection, features: pd.DataFrame) -> None:
    usable = len(db.resolved_bus_dates(conn, features, st.session_state.model_state))
    crossed_seed = usable == SEED_LABEL_THRESHOLD
    crossed_interval = (
        usable > SEED_LABEL_THRESHOLD
        and (usable - SEED_LABEL_THRESHOLD) % RETRAIN_INTERVAL == 0
    )
    if crossed_seed or crossed_interval:
        with st.spinner(f"Retraining on {usable} resolved bus-dates..."):
            _run_training_cycle(conn)


def _handle_decision(
    conn: psycopg.Connection,
    features: pd.DataFrame,
    *,
    bus_id: str,
    date: datetime.date,
    trip_id: int,
    device_id: str | None,
    decision: db.Decision,
    mode: db.Mode,
    n_candidates: int,
) -> None:
    db.insert_trip_label(
        conn,
        bus_id=bus_id,
        date=date,
        trip_id=trip_id,
        device_id=device_id,
        decision=decision,
        mode=mode,
        n_candidates=n_candidates,
    )
    _maybe_retrain(conn, features)
    st.session_state.current = None
    st.rerun()


def _render_contested_decisions(
    candidates: pd.DataFrame, decide: Callable[[str | None, db.Decision], None]
) -> None:
    for row in candidates.itertuples(index=False):
        score_str = f"{row.score:.2f}" if pd.notna(row.score) else "?"
        dict_badge = " · dictionary pick" if row.from_dictionary else ""
        st.caption(f"score {score_str}{dict_badge}")
        if st.button(row.device_id, key=f"pick_{row.device_id}", width="stretch"):
            decide(row.device_id, "match")
    if st.button("None of these", width="stretch"):
        decide(None, "none_of_these")
    if st.button("Unsure", width="stretch"):
        decide(None, "unsure")


def _render_uncontested_decision(
    top_device: str | None, decide: Callable[[str | None, db.Decision], None]
) -> None:
    if st.button(
        "Yes, matches this trip",
        type="primary",
        disabled=top_device is None,
        width="stretch",
    ):
        decide(top_device, "match")
    if st.button("No", width="stretch"):
        decide(None, "none_of_these")
    if st.button("Unsure", width="stretch"):
        decide(None, "unsure")


def _render_sidebar_decisions(
    conn: psycopg.Connection,
    features: pd.DataFrame,
    *,
    bus_id: str,
    date: datetime.date,
    trip_id: int,
    candidates: pd.DataFrame,
    is_contested: bool,
) -> None:
    device_ids = candidates["device_id"].tolist()
    mode: db.Mode = "contested" if is_contested else "uncontested"

    def decide(device_id: str | None, decision: db.Decision) -> None:
        _handle_decision(
            conn,
            features,
            bus_id=bus_id,
            date=date,
            trip_id=trip_id,
            device_id=device_id,
            decision=decision,
            mode=mode,
            n_candidates=len(device_ids),
        )

    st.caption(
        "Which candidate looks right for THIS trip?"
        if is_contested
        else "Does this device match THIS trip?"
    )
    if is_contested:
        _render_contested_decisions(candidates, decide)
    else:
        top_device = device_ids[0] if device_ids else None
        _render_uncontested_decision(top_device, decide)

    if st.button("Skip this trip", width="stretch"):
        st.session_state.skipped_trips.add(trip_id)
        st.session_state.current = None
        st.rerun()

    if st.button("Get a different bus/route", width="stretch"):
        st.session_state.skipped_bus_dates.add((bus_id, date))
        st.session_state.current = None
        st.rerun()


_STOPPING_VERDICT_RENDER = {
    "too_early": st.info,
    "keep_going": st.info,
    "slowing": st.warning,
    "stop_soon": st.success,
    "check_features": st.error,
}


def _render_status_panel(conn: psycopg.Connection, features: pd.DataFrame) -> None:
    """Progress, model status, and the auto stopping-criteria signal.

    Deliberately in the main panel, not the sidebar -- the sidebar is
    reserved for the clickable decision controls only.
    """
    with st.expander("Status: progress, model, stopping signal", expanded=True):
        trip_counts = db.trip_label_counts(conn)
        model_state = st.session_state.model_state
        n_resolved = len(db.resolved_bus_dates(conn, features, model_state))
        n_coverage = db.model_coverage_count(conn, features, model_state)
        n_total = get_total_bus_dates(conn)

        st.caption(
            f"**{trip_counts['total']} trip decisions** · "
            f"match {trip_counts['match']} · "
            f"none-of-these {trip_counts['none_of_these']} · "
            f"unsure {trip_counts['unsure']}"
        )
        st.caption(
            f"**{n_resolved} bus-dates resolved by labeling** "
            "(training data -- meant to stay small, a few hundred per the plan)"
        )
        st.caption(
            f"**{n_coverage} / {n_total} bus-dates** the prior is confident about "
            "with zero votes (the real coverage number -- climbs as the model "
            "improves, without you voting on all of them)"
        )
        st.progress(min(n_coverage / n_total, 1.0) if n_total else 0.0)

        n_features_dates = features["date"].nunique() if not features.empty else 0
        st.caption(f"Tier 1 features available for {n_features_dates} dates so far")
        st.caption(
            "Selection: model-informed uncertainty sampling"
            if model_state is not None
            else "Selection: Tier 1 heuristic-informed uncertainty sampling "
            "(no model yet)"
        )

        if model_state is None:
            to_go = max(SEED_LABEL_THRESHOLD - n_resolved, 0)
            st.caption(
                f"No model trained yet ({to_go} more resolved bus-dates "
                "until the first retrain)."
                if to_go
                else "First retrain due any label now."
            )
        else:
            run_row = model_state.get("run_row")
            if run_row:
                st.caption(
                    f"Model: {run_row['n_train_labels']} train rows · "
                    f"test AUC {run_row['test_auc']:.2f} · "
                    f"test Brier {run_row['test_brier']:.3f}"
                )
            model_runs = db.fetch_model_runs(conn)
            if len(model_runs) > 1:
                st.line_chart(
                    model_runs.set_index("n_train_labels")[["test_brier", "test_auc"]]
                )

        signal = db.stopping_signal(conn)
        render_fn = _STOPPING_VERDICT_RENDER.get(signal["verdict"], st.info)
        render_fn(f"**Stopping signal**: {signal['message']}")


def _init_session_state(conn: psycopg.Connection) -> None:
    if "current" not in st.session_state:
        st.session_state.current = None
    if "skipped_trips" not in st.session_state:
        st.session_state.skipped_trips = set()
    if "skipped_bus_dates" not in st.session_state:
        st.session_state.skipped_bus_dates = set()
    if "model_state" not in st.session_state:
        st.session_state.model_state = _load_latest_model(conn)
    if "selection_mode" not in st.session_state:
        st.session_state.selection_mode = "hardest"


def _render_sidebar(
    conn: psycopg.Connection, features: pd.DataFrame, current: tuple | None
) -> None:
    """Sidebar holds only the clickable decision controls, on request.

    Streamlit keeps it fixed in place while the main panel scrolls, so
    this is what actually stays reachable without hunting for it.
    Everything informational (counts, model status, stopping signal)
    lives in the main panel's status section instead.
    """
    with st.sidebar:
        st.radio(
            "Next-pick mode",
            options=["hardest", "random"],
            format_func=lambda m: (
                "Hardest case (default)" if m == "hardest" else "Random bus-date"
            ),
            key="selection_mode",
            on_change=_on_selection_mode_change,
            help=(
                "Hardest: uncertainty sampling, most informative for the model. "
                "Random: sweeps up ordinary/easy cases too, to counter decision-"
                "boundary jitter from labeling only hard cases."
            ),
        )
        st.divider()
        if current is None:
            st.success("Nothing left to label right now.")
            return
        bus_id, date, trip_id, is_contested = current
        candidates = db.fetch_bus_date_candidates(
            conn, features, bus_id, date, model_state=st.session_state.model_state
        )
        _render_sidebar_decisions(
            conn,
            features,
            bus_id=bus_id,
            date=date,
            trip_id=trip_id,
            candidates=candidates,
            is_contested=is_contested,
        )


def _render_shape_caption(shapes: dict[str, Any]) -> None:
    if not shapes:
        st.caption(
            "No matched GTFS shape for this trip -- no route to compare against."
        )
    elif len(shapes) == 1:
        st.caption(
            f"Only direction {next(iter(shapes))} exists in GTFS for this route."
        )
    else:
        st.caption("Both directions I (blue) and V (orange) shown below.")


def _build_map_shapes(
    conn: psycopg.Connection, trip: pd.Series, shapes: dict[str, Any]
) -> dict[str, tuple[list[list[float]] | None, pd.DataFrame]]:
    map_shapes: dict[str, tuple[list[list[float]] | None, pd.DataFrame]] = {}
    for direction, shape_id_col in (("I", "gtfs_shape_id_i"), ("V", "gtfs_shape_id_v")):
        if direction not in shapes:
            continue
        shape_id = trip[shape_id_col]
        geojson = db.fetch_shape_geojson(conn, trip["gtfs_feed_version_date"], shape_id)
        stops = db.fetch_route_stops(conn, trip["gtfs_feed_version_date"], shape_id)
        map_shapes[direction] = (geojson, stops)
    return map_shapes


def _render_candidate_maps(
    *,
    bus_id: str,
    date: datetime.date,
    trip_id: int,
    candidates: pd.DataFrame,
    is_contested: bool,
    map_shapes: dict[str, tuple[list[list[float]] | None, pd.DataFrame]],
    tracks: dict[str, pd.DataFrame],
) -> None:
    map_candidates = candidates if is_contested else candidates.head(1)
    st.caption(
        "One map per candidate per GTFS direction -- ordered best to worst score, "
        "gray-to-black trail is trip start to trip end"
        if is_contested
        else "Maps for the top-scored candidate -- gray-to-black trail is trip "
        "start to trip end"
    )
    for row in map_candidates.itertuples(index=False):
        device_id = row.device_id
        score_str = f"{row.score:.2f}" if pd.notna(row.score) else "not yet scored"
        dict_badge = " · dictionary pick" if row.from_dictionary else ""
        st.markdown(f"**{device_id}** &mdash; score {score_str}{dict_badge}")
        if not map_shapes:
            st.caption("No matched GTFS shape for this trip.")
            continue

        map_cols = st.columns(len(map_shapes))
        for col, direction in zip(map_cols, map_shapes, strict=False):
            with col:
                st.caption(f"Map {direction}")
                streamlit_folium.st_folium(
                    maps.build_candidate_map(
                        {direction: map_shapes[direction]}, tracks[device_id]
                    ),
                    height=MAP_HEIGHT_PX,
                    use_container_width=True,
                    returned_objects=[],
                    key=f"map_{bus_id}_{date}_{trip_id}_{device_id}_{direction}",
                )
        st.divider()


def main() -> None:
    """Render the labeling UI."""
    conn = get_connection()
    shape_cache = get_shape_cache(conn)
    features = get_features()

    _init_session_state(conn)
    _ensure_current_trip(conn, features)
    current = st.session_state.current

    _render_sidebar(conn, features, current)
    _render_status_panel(conn, features)

    if current is None:
        st.info(
            "Nothing left to label right now -- check back once more dates "
            "are featurized."
        )
        return
    bus_id, date, trip_id, is_contested = current

    trips = db.fetch_bus_date_trips(conn, bus_id, date)
    trip_rows = trips[trips["trip_id"] == trip_id]
    if trip_rows.empty:
        st.session_state.current = None
        st.rerun()
        return
    trip = trip_rows.iloc[0]

    candidates = db.fetch_bus_date_candidates(
        conn, features, bus_id, date, model_state=st.session_state.model_state
    )
    device_ids = candidates["device_id"].tolist()

    sampled = select_sample_trips(trips)
    labeled_here = db.labeled_trip_ids(conn) & set(sampled["trip_id"])

    st.subheader(
        f"Bus {bus_id} · {date} · {'CONTESTED' if is_contested else 'uncontested'}"
    )
    start_str = f"{trip['trip_start_timestamp']:%H:%M:%S}"
    end_str = f"{trip['trip_end_timestamp']:%H:%M:%S}"
    st.caption(
        f"This trip: route {trip['route_id']} · {start_str} - {end_str}"
        f"  ·  {len(labeled_here)}/{len(sampled)} sampled trips labeled "
        "for this bus-date so far"
        "  ·  decide in the sidebar"
    )

    shapes = _available_shapes(trip, shape_cache)
    _render_shape_caption(shapes)

    tracks: dict[str, pd.DataFrame] = {
        device_id: db.fetch_device_trip_positions(
            conn, device_id, trip["trip_start_timestamp"], trip["trip_end_timestamp"]
        )
        for device_id in device_ids
    }
    map_shapes = _build_map_shapes(conn, trip, shapes)

    _render_candidate_maps(
        bus_id=bus_id,
        date=date,
        trip_id=trip_id,
        candidates=candidates,
        is_contested=is_contested,
        map_shapes=map_shapes,
        tracks=tracks,
    )


main()
