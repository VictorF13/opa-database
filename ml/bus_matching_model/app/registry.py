"""Persists trained models/calibrators to disk and their metadata to Postgres."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import db
import joblib
from calibration import PlattCalibrator

if TYPE_CHECKING:
    import lightgbm as lgb
    import psycopg

ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "artifacts" / "models"


def save_run(
    conn: psycopg.Connection,
    *,
    n_train_labels: int,
    model: lgb.LGBMClassifier,
    calibrator: PlattCalibrator,
    selected_features: list[str],
    hyperparameters: dict[str, Any],
    test_metrics: dict[str, float],
    n_resolved_bus_dates: int | None = None,
    n_trip_labels_total: int | None = None,
) -> int:
    """Serialize a trained model + calibrator to disk and record its metadata.

    Args:
        conn: An open connection.
        n_train_labels: Training pool size (row count, post-expansion)
            at this run.
        model: The fitted LightGBM classifier.
        calibrator: The fitted Platt calibrator.
        selected_features: Feature columns the model was fit on (always
            every Tier 1 feature -- see plan Section 5, no selection).
        hyperparameters: The fixed hyperparameters used.
        test_metrics: Output of `metrics.evaluate` on the frozen test set.
        n_resolved_bus_dates: Resolved bus-date count *at this retrain*
            -- feeds `db.stopping_signal`'s growth-rate check.
        n_trip_labels_total: Total trip-level decisions *at this
            retrain* -- same purpose.

    Returns:
        The new `ml.bus_matching_model_runs` row's `run_id`.

    """
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    artifact_path = (
        ARTIFACTS_DIR / f"run_{n_train_labels:04d}_{int(time.time())}.joblib"
    )
    # Stored as plain data plus the calibrator's underlying sklearn model,
    # never a PlattCalibrator instance directly -- pickling that ties the
    # artifact to *this exact* class object, which breaks if this app's
    # modules get hot-reloaded (Streamlit's file watcher) between when
    # the instance was created and when it's pickled here.
    joblib.dump(
        {
            "model": model,
            "calibrator_model": calibrator.to_sklearn_model(),
            "selected_features": selected_features,
            "hyperparameters": hyperparameters,
        },
        artifact_path,
    )

    return db.insert_model_run(
        conn,
        {
            "run_type": "cycle",
            "n_train_labels": n_train_labels,
            "hyperparameters": hyperparameters,
            "selected_features": selected_features,
            "calibration_params": calibrator.to_params(),
            "cv_brier_score": None,
            "test_auc": test_metrics["auc"],
            "test_brier": test_metrics["brier"],
            "test_log_loss": test_metrics["log_loss"],
            "test_ece": test_metrics["ece"],
            "artifact_path": str(artifact_path),
            "n_resolved_bus_dates": n_resolved_bus_dates,
            "n_trip_labels_total": n_trip_labels_total,
        },
    )


def load_run(run_row: dict[str, Any]) -> dict[str, Any]:
    """Load a run's serialized model + calibrator + feature list from disk.

    Args:
        run_row: A row dict as returned by `db.fetch_latest_model_run`.

    Returns:
        Dict with keys "model", "calibrator", "selected_features".

    """
    artifact = joblib.load(run_row["artifact_path"])
    return {
        "model": artifact["model"],
        "calibrator": PlattCalibrator.from_sklearn_model(artifact["calibrator_model"]),
        "selected_features": artifact["selected_features"],
    }
