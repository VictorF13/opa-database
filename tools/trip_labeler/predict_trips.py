"""Batch inference using the currently-trained validity/candidate/reason models.

Scores every trip window already computed by 01c_score_device_mapping.py /
01d_score_vehicle_mapping.py within DATE_START/DATE_END, using the exact
same feature_vector formula (scoring.compute_feature_vector) those models
were trained on. Before writing anything, it cross-checks a random sample of
its own vectorized (pandas) feature computation against the canonical
scalar implementation - if those two ever disagree, the run aborts rather
than silently writing predictions built on a different feature vector than
the one the model actually learned.

Writes to scratch.trip_match_predictions: full TRUNCATE + reload each run
(a snapshot tied to one model checkpoint, not incremental - rerun after
retraining to refresh). Does not touch scratch.device_mapping_trip_scores /
scratch.vehicle_mapping_trip_scores or any gold table.

Run with: uv run tools/trip_labeler/predict_trips.py
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import psycopg
from scoring import (
    SOURCES,
    Candidate,
    compute_feature_vector,
    load_iv_overlap,
    load_shape_start_end_dist,
)

DSN = "postgresql://opa:opa@localhost:5432/opa"
MODEL_DIR = Path(__file__).parent / "model_store"

DATE_START = "2023-11-01"
DATE_END = "2023-12-01"

FEATURE_COLUMNS = [
    "avg_dist_to_line_m",
    "progress_corr",
    "start_proximity_m",
    "end_proximity_m",
    "speed_percentile",
    "implied_speed_kmh",
    "n_pings_in_window",
    "gap",
    "iv_overlap_m",
    "shape_start_end_dist_m",
]
KEY_COLUMNS = ["source", "entity_id", "trip_opened_at"]
SPOT_CHECK_SAMPLE = 300
VALID_HIGH_CONF = 0.8
VALID_LOW_CONF = 0.2
PROBABILITY_THRESHOLD = 0.5
SINGLE_CANDIDATE = 1


def fetch_candidates(conn: psycopg.Connection, source: str) -> pd.DataFrame:
    """Pull every raw candidate row for `source` within [DATE_START, DATE_END)."""
    cfg = SOURCES[source]
    # table/key_col below come only from the fixed SOURCES dict (never user
    # input); every actual value is a bind parameter, same pattern as app.py.
    sql = f"""
        SELECT {cfg["key_col"]}::text AS entity_id, trip_opened_at, trip_closed_at,
               resolved_feed_version_date, vehicle_number, shape_id, line_number,
               direction_id, avg_dist_to_line_m, progress_corr, start_proximity_m,
               end_proximity_m, speed_percentile, implied_speed_kmh,
               n_pings_in_window
        FROM {cfg["table"]}
        WHERE trip_opened_at >= %(start)s AND trip_opened_at < %(end)s
    """  # noqa: S608
    df = pd.read_sql(sql, conn, params={"start": DATE_START, "end": DATE_END})
    df["source"] = source
    return df


def pick_best_and_runner_up(raw: pd.DataFrame) -> pd.DataFrame:
    """Collapse each window's candidate rows into one row per window.

    The best (smallest avg_dist_to_line_m, NaN last) plus the runner-up's
    shape_id/direction_id/avg_dist_to_line_m, if a second candidate exists.
    """
    ordered = raw.sort_values(
        [*KEY_COLUMNS, "avg_dist_to_line_m"], na_position="last", kind="mergesort"
    )
    ordered["rank"] = ordered.groupby(KEY_COLUMNS, sort=False).cumcount()

    best = ordered[ordered["rank"] == 0].drop(columns="rank")
    runner_up = ordered.loc[
        ordered["rank"] == 1,
        [*KEY_COLUMNS, "avg_dist_to_line_m", "shape_id", "direction_id"],
    ].rename(
        columns={
            "avg_dist_to_line_m": "runner_up_avg_dist_to_line_m",
            "shape_id": "runner_up_shape_id",
            "direction_id": "runner_up_direction_id",
        }
    )
    merged = best.merge(runner_up, on=KEY_COLUMNS, how="left")
    merged["n_candidates"] = merged["runner_up_shape_id"].notna().astype(int) + 1
    merged["gap"] = (
        merged["runner_up_avg_dist_to_line_m"] - merged["avg_dist_to_line_m"]
    )
    return merged


def attach_geometry_features(
    df: pd.DataFrame,
    iv_overlap_m: dict[tuple[Any, str], float],
    shape_start_end_dist_m: dict[tuple[Any, str, str], float],
) -> pd.DataFrame:
    """Vectorized version of the two dict .get() lookups in compute_feature_vector.

    Uses a merge instead of per-row dict access, which is what makes this
    fast enough at ~1M+ rows.
    """
    iv_df = pd.DataFrame(
        [(k[0], k[1], v) for k, v in iv_overlap_m.items()],
        columns=["resolved_feed_version_date", "line_number", "iv_overlap_m"],
    )
    dist_df = pd.DataFrame(
        [(k[0], k[1], k[2], v) for k, v in shape_start_end_dist_m.items()],
        columns=[
            "resolved_feed_version_date",
            "line_number",
            "shape_id",
            "shape_start_end_dist_m",
        ],
    )
    df = df.merge(iv_df, on=["resolved_feed_version_date", "line_number"], how="left")
    dist_keys = ["resolved_feed_version_date", "line_number", "shape_id"]
    return df.merge(dist_df, on=dist_keys, how="left")


def spot_check_features(
    raw: pd.DataFrame,
    merged: pd.DataFrame,
    iv_overlap_m: dict[tuple[Any, str], float],
    shape_start_end_dist_m: dict[tuple[Any, str, str], float],
) -> None:
    """Recompute a random sample via the canonical scalar feature vector.

    Compares scoring.compute_feature_vector's output against this script's
    vectorized (pandas) computation and aborts the run on any mismatch -
    the guard against the vectorized reimplementation silently drifting
    from what the deployed models were actually trained on.
    """
    sample_idx = random.sample(range(len(merged)), min(SPOT_CHECK_SAMPLE, len(merged)))
    raw_by_key = {
        key: grp.to_dict("records") for key, grp in raw.groupby(KEY_COLUMNS, sort=False)
    }
    mismatches = 0
    for i in sample_idx:
        row = merged.iloc[i]
        key = tuple(row[c] for c in KEY_COLUMNS)
        candidates = [
            Candidate(
                shape_id=r["shape_id"],
                line_number=r["line_number"],
                direction_id=r["direction_id"],
                avg_dist_to_line_m=r["avg_dist_to_line_m"],
                progress_corr=r["progress_corr"],
                start_proximity_m=r["start_proximity_m"],
                end_proximity_m=r["end_proximity_m"],
                speed_percentile=r["speed_percentile"],
                implied_speed_kmh=r["implied_speed_kmh"],
                n_pings_in_window=r["n_pings_in_window"],
                duration_sec=None,
            )
            for r in raw_by_key[key]
        ]
        expected = compute_feature_vector(
            candidates,
            row["resolved_feed_version_date"],
            iv_overlap_m,
            shape_start_end_dist_m,
        )
        actual = row[FEATURE_COLUMNS].to_numpy(dtype=float)
        if not np.allclose(expected, actual, equal_nan=True):
            mismatches += 1
            print(f"  MISMATCH at {key}: expected {expected}, got {actual}")
    if mismatches:
        msg = (
            f"{mismatches}/{len(sample_idx)} spot-checked rows disagree with "
            "scoring.compute_feature_vector - aborting, nothing written"
        )
        raise RuntimeError(msg)
    print(f"  spot check passed: {len(sample_idx)}/{len(sample_idx)} rows match")


def ensure_predictions_table(conn: psycopg.Connection) -> None:
    """Create scratch.trip_match_predictions if it doesn't exist yet."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scratch.trip_match_predictions (
            source text NOT NULL,
            entity_id text NOT NULL,
            trip_opened_at timestamptz NOT NULL,
            trip_closed_at timestamptz NOT NULL,
            line_number text,
            vehicle_number text,
            resolved_feed_version_date date,
            n_candidates smallint NOT NULL,
            predicted_valid boolean NOT NULL,
            valid_probability double precision NOT NULL,
            predicted_shape_id text,
            candidate_probability double precision,
            predicted_invalid_reason text,
            invalid_reason_probability double precision,
            trust_tier text NOT NULL,
            model_trained_n integer NOT NULL,
            predicted_at timestamptz NOT NULL,
            PRIMARY KEY (source, entity_id, trip_opened_at)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS trip_match_predictions_trust_tier_idx "
        "ON scratch.trip_match_predictions (trust_tier)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS trip_match_predictions_line_number_idx "
        "ON scratch.trip_match_predictions (line_number)"
    )


def _add_predictions(
    merged: pd.DataFrame,
    validity_model: Any,  # noqa: ANN401
    candidate_model: Any,  # noqa: ANN401
    reason_model: Any,  # noqa: ANN401
    model_trained_n: int,
) -> pd.DataFrame:
    x = merged[FEATURE_COLUMNS].to_numpy(dtype=float)
    merged["valid_probability"] = validity_model.predict_proba(x)[:, 1]
    merged["predicted_valid"] = merged["valid_probability"] >= PROBABILITY_THRESHOLD

    p_naive_best = candidate_model.predict_proba(x)[:, 1]
    naive_best_wins = (merged["n_candidates"] == SINGLE_CANDIDATE) | (
        p_naive_best >= PROBABILITY_THRESHOLD
    )
    merged["predicted_shape_id"] = np.where(
        naive_best_wins, merged["shape_id"], merged["runner_up_shape_id"]
    )
    merged["candidate_probability"] = np.where(
        merged["n_candidates"] == SINGLE_CANDIDATE,
        np.nan,
        np.where(naive_best_wins, p_naive_best, 1 - p_naive_best),
    )

    # only meaningful when predicted_valid is False - see LabeledExample.invalid_reason
    # in app.py for the same 1=NOT_THIS_ROUTE / 0=INVALID_TRIP convention.
    p_not_this_route = reason_model.predict_proba(x)[:, 1]
    not_this_route_wins = p_not_this_route >= PROBABILITY_THRESHOLD
    reason_labels = pd.Series(
        np.where(not_this_route_wins, "NOT_THIS_ROUTE", "INVALID_TRIP"),
        index=merged.index,
    )
    merged["predicted_invalid_reason"] = reason_labels.where(~merged["predicted_valid"])
    merged["invalid_reason_probability"] = np.where(
        merged["predicted_valid"],
        np.nan,
        np.where(not_this_route_wins, p_not_this_route, 1 - p_not_this_route),
    )

    conditions = [
        merged["predicted_valid"] & (merged["valid_probability"] >= VALID_HIGH_CONF),
        merged["predicted_valid"] & (merged["valid_probability"] < VALID_HIGH_CONF),
        (~merged["predicted_valid"]) & (merged["valid_probability"] > VALID_LOW_CONF),
        (~merged["predicted_valid"]) & (merged["valid_probability"] <= VALID_LOW_CONF),
    ]
    choices = [
        "high_confidence_valid",
        "low_confidence_valid",
        "low_confidence_invalid",
        "high_confidence_invalid",
    ]
    merged["trust_tier"] = np.select(conditions, choices, default="unknown")
    merged["model_trained_n"] = model_trained_n
    merged["predicted_at"] = datetime.now(UTC)
    return merged


def _write_predictions(conn: psycopg.Connection, merged: pd.DataFrame) -> None:
    ensure_predictions_table(conn)
    conn.execute("TRUNCATE scratch.trip_match_predictions")

    out_cols = [
        "source",
        "entity_id",
        "trip_opened_at",
        "trip_closed_at",
        "line_number",
        "vehicle_number",
        "resolved_feed_version_date",
        "n_candidates",
        "predicted_valid",
        "valid_probability",
        "predicted_shape_id",
        "candidate_probability",
        "predicted_invalid_reason",
        "invalid_reason_probability",
        "trust_tier",
        "model_trained_n",
        "predicted_at",
    ]
    out = merged[out_cols].astype(object)
    out = out.where(out.notna(), None)

    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY scratch.trip_match_predictions "
            "(source, entity_id, trip_opened_at, trip_closed_at, line_number, "
            "vehicle_number, resolved_feed_version_date, n_candidates, "
            "predicted_valid, valid_probability, predicted_shape_id, "
            "candidate_probability, predicted_invalid_reason, "
            "invalid_reason_probability, trust_tier, "
            "model_trained_n, predicted_at) FROM STDIN"
        ) as copy,
    ):
        for row in out.itertuples(index=False, name=None):
            copy.write_row(row)

    print(f"  {len(out)} rows written")
    print(merged["trust_tier"].value_counts())


def main() -> None:
    """Score every trip window in [DATE_START, DATE_END) and write predictions."""
    conn = psycopg.connect(DSN, autocommit=True)

    print(f"loading models from {MODEL_DIR}")
    validity_model = joblib.load(MODEL_DIR / "validity_model.joblib")
    candidate_model = joblib.load(MODEL_DIR / "candidate_model.joblib")
    reason_model = joblib.load(MODEL_DIR / "reason_model.joblib")
    meta = json.loads((MODEL_DIR / "validity_meta.json").read_text())
    model_trained_n = meta["n_examples"]

    print("loading static route geometry")
    iv_overlap_m = load_iv_overlap(conn)
    shape_start_end_dist_m = load_shape_start_end_dist(conn)

    print(f"fetching candidate rows [{DATE_START}, {DATE_END})")
    raw = pd.concat([fetch_candidates(conn, s) for s in SOURCES], ignore_index=True)
    print(f"  {len(raw)} raw candidate rows")

    merged = pick_best_and_runner_up(raw)
    merged = attach_geometry_features(merged, iv_overlap_m, shape_start_end_dist_m)
    print(f"  {len(merged)} trip windows")

    print("spot-checking vectorized features against scoring.compute_feature_vector")
    spot_check_features(raw, merged, iv_overlap_m, shape_start_end_dist_m)

    merged = _add_predictions(
        merged, validity_model, candidate_model, reason_model, model_trained_n
    )

    print("writing scratch.trip_match_predictions")
    _write_predictions(conn, merged)

    conn.close()


if __name__ == "__main__":
    main()
