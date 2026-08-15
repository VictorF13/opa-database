"""Persists trained models/calibrators to disk and their metadata to Postgres."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import db
import joblib
from calibration import PlattCalibrator
from training import ModelConfig

if TYPE_CHECKING:
    import lightgbm as lgb
    import psycopg

ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "artifacts"


def save_run(
    conn: psycopg.Connection,
    *,
    run_type: str,
    n_train_labels: int,
    model: lgb.LGBMClassifier,
    calibrator: PlattCalibrator,
    config: ModelConfig,
    cv_brier_score: float | None,
    test_metrics: dict[str, float],
    confident_90_count: int,
    confident_90_total: int,
) -> int:
    """Serialize a trained model + calibrator to disk and record its metadata.

    Args:
        conn: An open connection.
        run_type: "cycle" or "milestone".
        n_train_labels: Training pool size at this run.
        model: The fitted LightGBM classifier.
        calibrator: The fitted Platt calibrator.
        config: The feature subset + hyperparameters used.
        cv_brier_score: The milestone's CV score, or `None` for a plain cycle.
        test_metrics: Output of `metrics.evaluate` on the frozen test set.
        confident_90_count: Rows predicted with at least 90% confidence.
        confident_90_total: Rows scored for the confidence count.

    Returns:
        The new `ml.trip_validity_model_runs` row's `run_id`.

    """
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    artifact_path = (
        ARTIFACTS_DIR / f"run_{n_train_labels:04d}_{run_type}_{int(time.time())}.joblib"
    )
    # Stored as plain data (dict/list) plus the calibrator's underlying
    # third-party sklearn model, never a PlattCalibrator/ModelConfig
    # instance directly - pickling those ties the artifact to *that
    # exact* class object, which breaks if this app's own modules get
    # hot-reloaded (e.g. by Streamlit's file watcher during dev) between
    # when the instance was created and when it's pickled here.
    joblib.dump(
        {
            "model": model,
            "calibrator_model": calibrator.to_sklearn_model(),
            "selected_features": config.selected_features,
            "hyperparameters": config.hyperparameters,
        },
        artifact_path,
    )

    return db.insert_model_run(
        conn,
        {
            "run_type": run_type,
            "n_train_labels": n_train_labels,
            "hyperparameters": config.hyperparameters,
            "selected_features": config.selected_features,
            "calibration_params": calibrator.to_params(),
            "cv_brier_score": cv_brier_score,
            "test_auc": test_metrics["auc"],
            "test_brier": test_metrics["brier"],
            "test_log_loss": test_metrics["log_loss"],
            "test_ece": test_metrics["ece"],
            "confident_90_count": confident_90_count,
            "confident_90_total": confident_90_total,
            "artifact_path": str(artifact_path),
        },
    )


def load_run(run_row: dict[str, Any]) -> dict[str, Any]:
    """Load a run's serialized model + calibrator + config from disk.

    Reconstructs fresh `PlattCalibrator`/`ModelConfig` instances from the
    plain data `save_run` stored, using whichever class definitions are
    currently loaded - so this is safe to call regardless of how many
    times those modules have been hot-reloaded since the artifact was
    written.

    Args:
        run_row: A row dict as returned by `db.fetch_latest_model_run`.

    Returns:
        Dict with keys "model", "calibrator", "config".

    """
    artifact = joblib.load(run_row["artifact_path"])
    if "calibrator_model" in artifact:
        return {
            "model": artifact["model"],
            "calibrator": PlattCalibrator.from_sklearn_model(
                artifact["calibrator_model"]
            ),
            "config": ModelConfig(
                selected_features=artifact["selected_features"],
                hyperparameters=artifact["hyperparameters"],
            ),
        }
    # Backward compatibility: artifacts written before this module
    # switched to storing plain data instead of pickled
    # PlattCalibrator/ModelConfig instances directly.
    return artifact
