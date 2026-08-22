"""DDL bootstrap for the active learning app's own tables.

Only ever creates `ml.trip_validity_labels` and
`ml.trip_validity_model_runs`. Never touches `silver.*`, and the only
other `ml.*` object referenced is a read-only foreign key onto
`ml.trip_validity_dataset`, which belongs to the notebooks pipeline in
`ml/trip_validity_model/notebooks/`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

_LABELS_DDL = """
CREATE TABLE IF NOT EXISTS ml.trip_validity_labels (
    trip_id BIGINT PRIMARY KEY REFERENCES ml.trip_validity_dataset (trip_id),
    label BOOLEAN NOT NULL,
    label_set TEXT NOT NULL CHECK (label_set IN ('calibration', 'test', 'train')),
    selection_source TEXT NOT NULL CHECK (selection_source IN ('random', 'uncertain')),
    predicted_probability_at_label_time DOUBLE PRECISION,
    labeled_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_MODEL_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS ml.trip_validity_model_runs (
    run_id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_type TEXT NOT NULL CHECK (run_type IN ('cycle', 'milestone')),
    n_train_labels INTEGER NOT NULL,
    hyperparameters JSONB NOT NULL,
    selected_features JSONB NOT NULL,
    calibration_params JSONB,
    cv_brier_score DOUBLE PRECISION,
    test_auc DOUBLE PRECISION,
    test_brier DOUBLE PRECISION,
    test_log_loss DOUBLE PRECISION,
    test_ece DOUBLE PRECISION,
    confident_90_count INTEGER,
    confident_90_total INTEGER,
    artifact_path TEXT NOT NULL
);
"""

_INDEXES_DDL = """
CREATE INDEX IF NOT EXISTS trip_validity_labels_set_idx
    ON ml.trip_validity_labels (label_set);
CREATE INDEX IF NOT EXISTS trip_validity_model_runs_created_at_idx
    ON ml.trip_validity_model_runs (created_at);
"""


def ensure_schema(conn: psycopg.Connection) -> None:
    """Create the active learning app's tables if they don't already exist.

    Args:
        conn: An open connection to the database.

    """
    with conn.transaction():
        conn.execute(_LABELS_DDL)
        conn.execute(_MODEL_RUNS_DDL)
        conn.execute(_INDEXES_DDL)
