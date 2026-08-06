"""Batch inference over the full trip_finder candidate dataset.

Scores every candidate row currently sitting in data/batches/*.parquet with
the trained validity+completeness cascade, picks each trip's single best
candidate (highest predicted validity probability), and writes one row per
trip to a BRAND NEW table, scratch.trip_finder_predictions. Never touches
scratch.trip_finder_labels or any other existing table -- this is purely
new output, same spirit as trip_labeler/predict_trips.py writing to its own
scratch.trip_match_predictions.

Trip-level "no real match found" isn't a separate step: it falls straight
out of normal per-trip best-candidate selection. A trip's winning candidate
carries the SAME trust_tier convention trip_labeler/predict_trips.py uses
(high/low_confidence_valid/invalid, thresholded on VALID_HIGH_CONF/
VALID_LOW_CONF) -- if even the best candidate isn't confidently valid, that
trip is confidently invalid, exactly like "the other model" the user
referred to. Trips where every candidate has fewer than
MIN_PINGS_FOR_LABELING pings (no real GPS signal to judge from at all) are
auto-resolved the same way app.py's own coverage stat treats them: counted
as high_confidence_invalid without ever calling the model.

Meant to run once find_candidates.py has finished generating every batch
(see run_final_pipeline.py, which waits for that and then calls this) but
works fine on a partial batch set too for a sanity check -- it just scores
however many trips currently have candidate data.

Run with: uv run tools/trip_finder/predict_trips.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import joblib
import numpy as np
import polars as pl
import psycopg

if TYPE_CHECKING:
    from sklearn.ensemble import HistGradientBoostingClassifier

DSN = "postgresql://opa:opa@localhost:5432/opa"
MODEL_DIR = Path(__file__).parent / "model_store"
BATCHES_DIR = Path(__file__).parent / "data" / "batches"

# Kept in sync with app.py by hand, same as trip_labeler/predict_trips.py's
# own relationship to trip_labeler/app.py -- not imported from app.py
# directly, since importing it would execute its module-level
# `store = LabelStore(DSN)` and spin up the entire live-app machinery
# (Postgres connection, full retrain, pool fill) just to reuse a few
# constants, potentially colliding with an actually-running app.py.
NATURAL_KEY_COLUMNS = [
    "vehicle_number",
    "line_number",
    "trip_opened_at",
    "trip_closed_at",
    "candidate_vehicle_id",
]
FEATURE_COLUMNS = [
    "n_pings_in_window",
    "ida_avg_dist_to_line_m",
    "ida_progress_corr",
    "ida_start_proximity_m",
    "ida_end_proximity_m",
    "volta_avg_dist_to_line_m",
    "volta_progress_corr",
    "volta_start_proximity_m",
    "volta_end_proximity_m",
    "duration_sec",
    "day_of_week",
    "hour_of_trip_start",
    "hour_of_trip_end",
    "iv_overlap_m",
    "ida_shape_start_end_dist_m",
    "volta_shape_start_end_dist_m",
    "ida_implied_speed_kmh",
    "ida_speed_percentile",
    "volta_implied_speed_kmh",
    "volta_speed_percentile",
    "total_distance_m",
    "ping_timespan_sec",
    "spatial_dispersion_m",
]
MIN_PINGS_FOR_LABELING = 5
VALID_HIGH_CONF = 0.8
VALID_LOW_CONF = 0.2
PROBABILITY_THRESHOLD = 0.5


def _feature_matrix(df: pl.DataFrame) -> np.ndarray:
    return df.select(FEATURE_COLUMNS).to_numpy().astype(np.float64)


def load_all_candidates() -> pl.DataFrame:
    """Read every currently-generated batch, whatever's landed so far."""
    files = sorted(BATCHES_DIR.glob("*.parquet"))
    if not files:
        msg = f"no batch files found in {BATCHES_DIR}"
        raise RuntimeError(msg)
    return pl.read_parquet(files)


def score_best_per_trip(
    df: pl.DataFrame,
    validity_model: HistGradientBoostingClassifier,
    completeness_model: HistGradientBoostingClassifier | None,
) -> pl.DataFrame:
    """Score every qualifying candidate and collapse to one row per trip.

    Trips with zero qualifying (>=MIN_PINGS_FOR_LABELING) candidates are
    handled separately by the caller (see sparse_trip_rows) -- this only
    covers trips that have at least one candidate worth judging.
    """
    qualifying = df.filter(pl.col("n_pings_in_window") >= MIN_PINGS_FOR_LABELING)
    if len(qualifying) == 0:
        return qualifying

    p_valid = validity_model.predict_proba(_feature_matrix(qualifying))[:, 1]
    n_considered = qualifying.group_by(NATURAL_KEY_COLUMNS[:4]).agg(
        pl.len().cast(pl.Int64).alias("n_candidates_considered")
    )
    qualifying = qualifying.with_columns(pl.Series("_p_valid", p_valid))
    best = (
        qualifying.sort("_p_valid", descending=True)
        .group_by(NATURAL_KEY_COLUMNS[:4], maintain_order=True)
        .first()
        .join(n_considered, on=NATURAL_KEY_COLUMNS[:4])
    )

    is_valid = best["_p_valid"].to_numpy() >= PROBABILITY_THRESHOLD

    # completeness is only meaningful given a match -- masked to None
    # wherever the trip wasn't predicted valid, matching app.py's
    # _candidate_to_json (which only ever computes it under the same
    # condition in the first place; predict_proba here runs over every row
    # up front for vectorization, so the mask is applied after instead).
    completeness_values: list[str | None] = [None] * len(best)
    completeness_probs: list[float | None] = [None] * len(best)
    if completeness_model is not None:
        proba = completeness_model.predict_proba(_feature_matrix(best))
        classes = completeness_model.classes_
        best_class_idx = proba.argmax(axis=1)
        completeness_values = [
            str(classes[i]) if valid else None
            for valid, i in zip(is_valid, best_class_idx, strict=True)
        ]
        completeness_probs = [
            float(proba[row, i]) if valid else None
            for row, (valid, i) in enumerate(zip(is_valid, best_class_idx, strict=True))
        ]
    predicted_completeness = pl.Series(
        "predicted_completeness", completeness_values, dtype=pl.Utf8
    )
    completeness_probability = pl.Series(
        "completeness_probability", completeness_probs, dtype=pl.Float64
    )

    predicted_valid = best["_p_valid"] >= PROBABILITY_THRESHOLD
    conditions = [
        predicted_valid & (best["_p_valid"] >= VALID_HIGH_CONF),
        predicted_valid & (best["_p_valid"] < VALID_HIGH_CONF),
        (~predicted_valid) & (best["_p_valid"] > VALID_LOW_CONF),
        (~predicted_valid) & (best["_p_valid"] <= VALID_LOW_CONF),
    ]
    choices = [
        "high_confidence_valid",
        "low_confidence_valid",
        "low_confidence_invalid",
        "high_confidence_invalid",
    ]
    trust_tier = np.select(
        [c.to_numpy() for c in conditions], choices, default="unknown"
    )

    return best.with_columns(
        predicted_candidate_vehicle_id=pl.col("candidate_vehicle_id"),
        predicted_valid=predicted_valid,
        valid_probability=pl.col("_p_valid"),
        predicted_completeness=predicted_completeness,
        completeness_probability=completeness_probability,
        trust_tier=pl.Series(trust_tier),
    ).select(
        [
            *NATURAL_KEY_COLUMNS[:4],
            "predicted_candidate_vehicle_id",
            "predicted_valid",
            "valid_probability",
            "predicted_completeness",
            "completeness_probability",
            "trust_tier",
            "n_candidates_considered",
        ]
    )


def sparse_trip_rows(df: pl.DataFrame) -> pl.DataFrame:
    """Trips where every candidate is too sparse to judge -- auto-invalid, no model.

    Same convention as app.py's _n_sparse_trips/coverage: a trip like this
    is just as "confidently resolved" as one with a clear match, it's simply
    resolved as "no usable signal, so no match."
    """
    per_trip_max = df.group_by(NATURAL_KEY_COLUMNS[:4]).agg(
        pl.col("n_pings_in_window").max().alias("max_pings")
    )
    sparse = per_trip_max.filter(pl.col("max_pings") < MIN_PINGS_FOR_LABELING)
    if len(sparse) == 0:
        return sparse
    return sparse.select(NATURAL_KEY_COLUMNS[:4]).with_columns(
        predicted_candidate_vehicle_id=pl.lit(None, dtype=pl.Int32),
        predicted_valid=pl.lit(False),  # noqa: FBT003
        valid_probability=pl.lit(0.0),
        predicted_completeness=pl.lit(None, dtype=pl.Utf8),
        completeness_probability=pl.lit(None, dtype=pl.Float64),
        trust_tier=pl.lit("high_confidence_invalid"),
        n_candidates_considered=pl.lit(0, dtype=pl.Int64),
    )


def ensure_predictions_table(conn: psycopg.Connection) -> None:
    """Create scratch.trip_finder_predictions if it doesn't exist. Never alters it."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scratch.trip_finder_predictions (
            vehicle_number text NOT NULL,
            line_number text NOT NULL,
            trip_opened_at timestamptz NOT NULL,
            trip_closed_at timestamptz NOT NULL,
            predicted_candidate_vehicle_id integer,
            predicted_valid boolean NOT NULL,
            valid_probability double precision NOT NULL,
            predicted_completeness text,
            completeness_probability double precision,
            trust_tier text NOT NULL,
            n_candidates_considered integer NOT NULL,
            model_trained_n integer NOT NULL,
            predicted_at timestamptz NOT NULL,
            PRIMARY KEY (vehicle_number, line_number, trip_opened_at, trip_closed_at)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS trip_finder_predictions_trust_tier_idx "
        "ON scratch.trip_finder_predictions (trust_tier)"
    )


def write_predictions(
    conn: psycopg.Connection, result: pl.DataFrame, model_trained_n: int
) -> None:
    """Full TRUNCATE + reload -- tied to one model checkpoint, not incremental.

    Writes straight from polars (iter_rows), not via pandas -- pandas has no
    real nullable-int dtype, so an Int32 column with any nulls in it (like
    predicted_candidate_vehicle_id, null exactly for sparse/invalid trips
    with no real candidate) gets silently upcast to float64, and the null
    rows end up as strings like "45075.0" instead of clean integers or NULL,
    which COPY then rejects outright.
    """
    ensure_predictions_table(conn)
    conn.execute("TRUNCATE scratch.trip_finder_predictions")

    now = datetime.now(UTC)
    out = result.with_columns(
        model_trained_n=pl.lit(model_trained_n),
        predicted_at=pl.lit(now),
    )

    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY scratch.trip_finder_predictions "
            "(vehicle_number, line_number, trip_opened_at, trip_closed_at, "
            "predicted_candidate_vehicle_id, predicted_valid, valid_probability, "
            "predicted_completeness, completeness_probability, trust_tier, "
            "n_candidates_considered, model_trained_n, predicted_at) FROM STDIN"
        ) as copy,
    ):
        for row in out.iter_rows():
            copy.write_row(row)

    print(f"  {len(out)} rows written")
    print(result["trust_tier"].value_counts())


def main() -> None:
    """Score every currently-available trip and write the predictions table."""
    print("loading models")
    validity_model = joblib.load(MODEL_DIR / "validity_model.joblib")
    completeness_path = MODEL_DIR / "completeness_model.joblib"
    completeness_model = (
        joblib.load(completeness_path) if completeness_path.exists() else None
    )
    meta = json.loads((MODEL_DIR / "validity_meta.json").read_text())
    model_trained_n = meta["n_examples"]

    print("loading candidate batches")
    df = load_all_candidates()
    n_trips = df.select(NATURAL_KEY_COLUMNS[:4]).unique().height
    print(f"  {len(df)} candidate rows across {n_trips} trips")

    print("scoring qualifying candidates")
    scored = score_best_per_trip(df, validity_model, completeness_model)
    print(f"  {len(scored)} trips resolved via the model")

    print("resolving sparse (no-signal) trips")
    sparse = sparse_trip_rows(df)
    print(f"  {len(sparse)} trips auto-resolved as sparse/invalid")

    result = pl.concat([scored, sparse]) if len(sparse) else scored
    print(
        f"  {len(result)} total trips written ({n_trips - len(result)} unaccounted for)"
    )

    conn = psycopg.connect(DSN, autocommit=True)
    print("writing scratch.trip_finder_predictions")
    write_predictions(conn, result, model_trained_n)
    conn.close()


if __name__ == "__main__":
    main()
