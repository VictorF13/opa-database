"""DDL bootstrap for the Bus Matching active-learning app's own tables.

Only ever creates `ml.bus_matching_trip_labels`,
`ml.bus_matching_model_runs`, `ml.bus_matching_pair_labels` and
`ml.bus_matching_pair_model_runs`. Everything else this app reads
(`ml.bus_matching_candidates`, `ml.bus_matching_contestedness`,
`ml.trip_validity_final`, `ml.trip_validity_fares_final`,
`ml.trip_validity_route_shapes`, `ml.bus_matching_avl_positions`) is
read-only and belongs to earlier notebooks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

# One row per *trip*, not per bus-date: each decision is scoped to the
# single trip on screen ("which candidate looks right for THIS trip"),
# so a bus-date can accumulate several independent trip-level votes.
# `db.fetch_resolved_labels` turns these into a day-level verdict only
# once enough votes agree -- a single wrong click can never resolve a
# bus-date by itself, it needs a second, matching wrong click.
_TRIP_LABELS_DDL = """
CREATE TABLE IF NOT EXISTS ml.bus_matching_trip_labels (
    bus_id            TEXT NOT NULL,
    date              DATE NOT NULL,
    trip_id           BIGINT NOT NULL,
    device_id         TEXT,
    decision          TEXT NOT NULL CHECK (
                          decision IN ('match', 'none_of_these', 'unsure')
                      ),
    mode              TEXT NOT NULL CHECK (mode IN ('uncontested', 'contested')),
    n_candidates      INTEGER NOT NULL,
    labeled_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (bus_id, date, trip_id),
    CHECK (decision <> 'match' OR device_id IS NOT NULL)
);
"""

_MODEL_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS ml.bus_matching_model_runs (
    run_id               SERIAL PRIMARY KEY,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_type             TEXT NOT NULL CHECK (run_type IN ('cycle', 'milestone')),
    n_train_labels       INTEGER NOT NULL,
    hyperparameters      JSONB NOT NULL,
    selected_features    JSONB NOT NULL,
    calibration_params   JSONB,
    cv_brier_score       DOUBLE PRECISION,
    test_auc             DOUBLE PRECISION,
    test_brier           DOUBLE PRECISION,
    test_log_loss        DOUBLE PRECISION,
    test_ece             DOUBLE PRECISION,
    artifact_path        TEXT NOT NULL
);
"""

# Added after the table already existed in some environments (never had
# real rows before this, so a plain ADD COLUMN is safe) -- tracks the
# resolved-bus-date and total-trip-label counts *at each retrain*, which
# is what the stopping-criteria signal (`db.stopping_signal`) needs to
# see growth rate over time, not just the latest snapshot.
_MODEL_RUNS_MIGRATION_DDL = """
ALTER TABLE ml.bus_matching_model_runs
    ADD COLUMN IF NOT EXISTS n_resolved_bus_dates INTEGER,
    ADD COLUMN IF NOT EXISTS n_trip_labels_total INTEGER;
"""

# One row per *pair* for the whole month -- the unit the final
# deliverable is actually about ("is this device this bus's device"),
# and a different question from the trip-level table above. A device
# essentially never changes bus mid-month, so a month-level verdict is
# both answerable and far more informative per click than a per-date
# one: twenty days of evidence collapse into a single decision.
#
# `verdict` is deliberately three-way. "unsure" is a real, useful
# answer here -- it keeps a genuinely ambiguous pair out of the
# training set instead of forcing a coin-flip into it, and the
# selection logic can stop re-showing it.
_PAIR_LABELS_DDL = """
CREATE TABLE IF NOT EXISTS ml.bus_matching_pair_labels (
    bus_id            TEXT NOT NULL,
    device_id         TEXT NOT NULL,
    verdict           TEXT NOT NULL CHECK (
                          verdict IN ('correct', 'wrong', 'unsure')
                      ),
    was_top_candidate BOOLEAN NOT NULL,
    model_confidence  DOUBLE PRECISION,
    n_candidates      INTEGER NOT NULL,
    labeled_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (bus_id, device_id)
);
"""

# `label_source` separates the two sampling regimes, which measure
# genuinely different things and must never be pooled into one precision
# figure:
#
# - 'queue'  -- the ambiguity-ranked labeling queue. Deliberately biased
#               toward hard cases, so precision over these understates
#               the system.
# - 'audit'  -- a *random* sample of pairs the model is already confident
#               about (plan Section 7's "random confident pairs, small
#               but non-negotiable"). This is the only unbiased estimate
#               of whether confident predictions are actually right, and
#               the only one that should ever be quoted as "precision".
#
# Added after the table existed; existing rows are queue-sourced.
_PAIR_LABELS_MIGRATION_DDL = """
ALTER TABLE ml.bus_matching_pair_labels
    ADD COLUMN IF NOT EXISTS label_source TEXT NOT NULL DEFAULT 'queue';
"""

# Same shape as the day model's run table, kept separate so the two
# models' histories (and their trust ramps) never mix.
_PAIR_MODEL_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS ml.bus_matching_pair_model_runs (
    run_id               SERIAL PRIMARY KEY,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    n_train_labels       INTEGER NOT NULL,
    n_labeled_pairs      INTEGER NOT NULL,
    hyperparameters      JSONB NOT NULL,
    selected_features    JSONB NOT NULL,
    test_auc             DOUBLE PRECISION,
    test_brier           DOUBLE PRECISION,
    test_log_loss        DOUBLE PRECISION,
    test_ece             DOUBLE PRECISION,
    artifact_path        TEXT NOT NULL
);
"""

_INDEXES_DDL = """
CREATE INDEX IF NOT EXISTS bus_matching_trip_labels_bus_date_idx
    ON ml.bus_matching_trip_labels (bus_id, date);
CREATE INDEX IF NOT EXISTS bus_matching_model_runs_created_at_idx
    ON ml.bus_matching_model_runs (created_at);
CREATE INDEX IF NOT EXISTS bus_matching_pair_labels_bus_idx
    ON ml.bus_matching_pair_labels (bus_id);
CREATE INDEX IF NOT EXISTS bus_matching_pair_model_runs_created_at_idx
    ON ml.bus_matching_pair_model_runs (created_at);
"""

_DROP_OLD_LABELS_TABLE_DDL = """
DROP TABLE IF EXISTS ml.bus_matching_labels;
"""


def ensure_schema(conn: psycopg.Connection) -> None:
    """Create the active learning app's tables if they don't already exist.

    Args:
        conn: An open connection to the database.

    """
    with conn.transaction():
        # Superseded by bus_matching_trip_labels (trip-level, not
        # bus-date-level) -- never had real rows, safe to drop outright.
        conn.execute(_DROP_OLD_LABELS_TABLE_DDL)
        conn.execute(_TRIP_LABELS_DDL)
        conn.execute(_MODEL_RUNS_DDL)
        conn.execute(_MODEL_RUNS_MIGRATION_DDL)
        conn.execute(_PAIR_LABELS_DDL)
        conn.execute(_PAIR_LABELS_MIGRATION_DDL)
        conn.execute(_PAIR_MODEL_RUNS_DDL)
        conn.execute(_INDEXES_DDL)
