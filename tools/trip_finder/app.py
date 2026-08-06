"""Active-learning labeling app for the "which bus really did this trip" model.

Companion to tools/trip_labeler/app.py, but for a different, harder problem:
trip_labeler answers "does the trip's OWN recorded bus/device match one of its
own candidate GTFS shapes" for the ~95% of November-2023 AFC trips it could
resolve confidently. This app is for the leftover ~44,347 trips it could NOT
resolve either way, tackled by testing dozens of OTHER GPS-tracked vehicle_ids
per trip against that trip's own line (find_candidates.py builds this pool
locally from raw AVL pings, not from trip_labeler's candidate tables) and
deciding, per (trip, candidate vehicle) pair, whether that vehicle actually
ran the trip and in which direction.

Reads its candidate pool from tools/trip_finder/data/batches/*.parquet
(written incrementally by find_candidates.py, which may still be running in
the background -- refresh() picks up new batch files as they land) rather
than querying Postgres for candidates. It still touches Postgres for two
small, per-labeled-item things: raw pings and shape geometry for the map
display, and writing/reading rows in the new scratch.trip_finder_labels
table. Does not touch scratch.trip_match_labels, any trip_labeler model file,
or any gold table.

Three cascaded models, same shared 20-feature vector:
  - validity model: did this candidate vehicle actually run this trip, in
    some form? (target: decision in the 5 MATCH_DECISIONS, trained on every
    label)
  - completeness model: GIVEN it's a match, which of the 5 subtypes (
    IDA_FULL/IDA_PARTIAL/VOLTA_FULL/VOLTA_PARTIAL/BOTH)? (multiclass, trained
    only on MATCH decisions)
  - reason model: GIVEN it's not a match, DIFFERENT_ROUTE (a real, coherent
    trip - just not this one) or INVALID (not really following any coherent
    route)? (trained only on non-match decisions)
day_of_week/hour_of_trip_start/hour_of_trip_end are passed to
HistGradientBoostingClassifier as genuine categorical features (not plain
ints), via categorical_features=CATEGORICAL_MASK.

Two separate views of the same scored data, computed independently:
  - coverage stat (_best_per_trip): collapses to ONE row per trip, its
    current single best-scoring candidate -- the stopping criterion tracked
    in the UI header is the % of loaded trips whose best candidate has
    reached a high-confidence prediction, not a fixed label count.
  - labeling pool (_top_k_per_trip + _refill): draws each trip's top few
    candidates, not just rank 1 -- showing ONLY the best pick per trip meant
    the labeler almost never saw a genuine negative example (it's usually
    already right), so the validity model could never accumulate both
    classes. Once a model exists, three interleaved tracks (random,
    uncertain, and whichever of the 7 decision classes is rarest so far)
    keep it learning to predict well everywhere, not just at whatever
    boundary it already knows about.

Run with:
    uv run tools/trip_finder/app.py

Then open http://localhost:8011
"""

from __future__ import annotations

import json
import math
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import joblib
import numpy as np
import polars as pl
import psycopg
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import confusion_matrix, precision_recall_fscore_support
from sklearn.model_selection import (
    RandomizedSearchCV,
    StratifiedKFold,
    train_test_split,
)

DSN = "postgresql://opa:opa@localhost:5432/opa"
MODEL_DIR = Path(__file__).parent / "model_store"
BATCHES_DIR = Path(__file__).parent / "data" / "batches"

POOL_REFILL_AT = 40
MIN_LABELS_FOR_MODEL = 20
RETRAIN_EVERY = 10  # after the first fit, only refit every N new labels
TUNE_EVERY = 50  # only re-run hyperparameter search every N new labels
# The labeling pool draws from each trip's top-K candidates by current score,
# not just the single best one (that's _best_per_trip's job, used only for
# the coverage stat) -- showing ONLY rank 1 per trip meant the human almost
# never saw a genuine negative example, since the top pick is usually
# already right. Ranks 2..K for the same trip are the informative contrast
# set: plausible-looking but wrong, not random noise, and exactly what the
# validity model needs to ever see class 0 at all.
TOP_K_PER_TRIP = 5

PARAM_GRID: dict[str, list[Any]] = {
    "max_depth": [3, 4, 6, None],
    "learning_rate": [0.05, 0.1, 0.2],
    "max_iter": [50, 100, 200],
    "l2_regularization": [0.0, 0.5, 1.0, 2.0],
}
N_TUNE_ITER = 20
# See tools/trip_labeler/app.py's BASE_MODEL_KWARGS comment for why
# early_stopping is forced on and hyperparameter search is scored on
# log-loss, not accuracy -- same reasoning applies here unchanged.
BASE_MODEL_KWARGS: dict[str, Any] = {"early_stopping": True, "random_state": 0}

MIN_SAMPLES_PER_CLASS = 2
MIN_SAMPLES_FOR_TUNING = 10
PROBABILITY_THRESHOLD = 0.5
VALID_HIGH_CONF = 0.8  # trust_tier thresholds, matching trip_labeler/predict_trips.py
VALID_LOW_CONF = 0.2

# A candidate with fewer pings than this in its own trip window can't carry
# any real signal either way -- auto-decided INVALID and never shown to the
# labeler. A trip where every candidate is this sparse is auto-resolved
# (counted as confidently INVALID in the coverage stat) without needing a
# model at all.
MIN_PINGS_FOR_LABELING = 5

# trip_labeler's already-trained validity model answers a closely related
# question (does a candidate's ping behavior look like a coherent trip along
# a given shape) from the same underlying metrics, just laid out as
# "best candidate + gap to runner-up" instead of "ida view + volta view".
# Reused read-only, purely to rank the COLD-START pool by how promising a
# candidate looks (most-promising-first) instead of blind/naive ordering --
# most of these ~800 candidates/trip are going to be garbage, so surfacing
# likely positives early matters a lot for bootstrapping the rare MATCH
# class. Once this app's own validity model exists, it fully replaces this:
# the old model doesn't know about ida/volta full/partial/both at all, it
# was never trained on this problem's actual labels.
OLD_VALIDITY_MODEL_PATH = (
    Path(__file__).parent.parent
    / "trip_labeler"
    / "model_store"
    / "validity_model.joblib"
)
OLD_FEATURE_COLUMNS = [
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
CATEGORICAL_MASK = [
    c in ("day_of_week", "hour_of_trip_start", "hour_of_trip_end")
    for c in FEATURE_COLUMNS
]
BASE_MODEL_KWARGS["categorical_features"] = CATEGORICAL_MASK

NATURAL_KEY_COLUMNS = [
    "vehicle_number",
    "line_number",
    "trip_opened_at",
    "trip_closed_at",
    "candidate_vehicle_id",
]

MatchDecision = Literal[
    "IDA_FULL", "IDA_PARTIAL", "VOLTA_FULL", "VOLTA_PARTIAL", "BOTH"
]
ReasonDecision = Literal["DIFFERENT_ROUTE", "INVALID"]
Decision = MatchDecision | ReasonDecision
MATCH_DECISIONS: tuple[MatchDecision, ...] = (
    "IDA_FULL",
    "IDA_PARTIAL",
    "VOLTA_FULL",
    "VOLTA_PARTIAL",
    "BOTH",
)
REASON_DECISIONS: tuple[ReasonDecision, ...] = ("DIFFERENT_ROUTE", "INVALID")
ALL_DECISIONS: tuple[Decision, ...] = MATCH_DECISIONS + REASON_DECISIONS


def _clean(v: Any) -> Any:  # noqa: ANN401
    """Turn NaN into None so it survives standard JSON serialization."""
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


@dataclass
class LabeledExample:
    """One labeled (trip, candidate vehicle) pair, reduced to what the models need."""

    features: list[float]
    is_valid: int
    completeness_label: str | None  # one of MATCH_DECISIONS, only when is_valid
    reason_label: str | None  # one of REASON_DECISIONS, only when not is_valid


def _build_example(features: list[float], decision: str) -> LabeledExample:
    is_valid = decision in MATCH_DECISIONS
    return LabeledExample(
        features=features,
        is_valid=1 if is_valid else 0,
        completeness_label=decision if is_valid else None,
        reason_label=None if is_valid else decision,
    )


def _fittable(y: list[Any]) -> bool:
    """Whether a HistGradientBoostingClassifier can even be fit on y.

    Needs >= 2 distinct classes AND every class must have >=
    MIN_SAMPLES_PER_CLASS members. The member-count part specifically:
    early_stopping=True (see BASE_MODEL_KWARGS) makes fit() carve out its
    own internal stratified train/validation split, which raises if any
    class has only 1 member (it can't appear on both sides of that split).
    `len(set(y)) > 1` alone (checking distinctness, not member counts) looks
    like the right guard and mostly works, but crashes exactly when a rare
    class has only 1 label -- which is the ordinary state of a rare class
    early in labeling, not an edge case.
    """
    counts = Counter(y)
    return len(counts) > 1 and min(counts.values()) >= MIN_SAMPLES_PER_CLASS


def _to_x(features: list[list[float]]) -> np.ndarray:
    """Convert feature-vector rows into the array HistGradientBoostingClassifier needs.

    categorical_features (see CATEGORICAL_MASK) makes sklearn call X.shape
    before doing any conversion of its own -- a plain list of lists doesn't
    have .shape, so every .fit()/.predict()/.predict_proba() call on one of
    this app's own three models MUST go through here first, or it fails
    (silently, from the caller's point of view: RandomizedSearchCV just
    reports "all fits failed" and _tune_model swallows that into {}).
    """
    if not features:
        return np.empty((0, len(FEATURE_COLUMNS)))
    return np.asarray(features, dtype=np.float64)


def _valid_xy(examples: list[LabeledExample]) -> tuple[np.ndarray, list[int]]:
    return _to_x([e.features for e in examples]), [e.is_valid for e in examples]


def _completeness_xy(examples: list[LabeledExample]) -> tuple[np.ndarray, list[str]]:
    feats = [e.features for e in examples if e.completeness_label is not None]
    labels = [
        e.completeness_label for e in examples if e.completeness_label is not None
    ]
    return _to_x(feats), labels


def _reason_xy(examples: list[LabeledExample]) -> tuple[np.ndarray, list[str]]:
    feats = [e.features for e in examples if e.reason_label is not None]
    labels = [e.reason_label for e in examples if e.reason_label is not None]
    return _to_x(feats), labels


def _fit_model(
    x: np.ndarray, y: list[Any], params: dict[str, Any] | None = None
) -> HistGradientBoostingClassifier:
    """Fit one model, retrying once without early_stopping if that's what broke it.

    early_stopping=True (see BASE_MODEL_KWARGS) makes fit() carve out its
    own internal validation split sized off however much data THIS call
    got -- fine on the full label set, but deep inside nested CV a fold's
    training subset can be small enough that 10% of it can't even hold one
    example of each class, and sklearn raises rather than silently
    skipping early stopping. That's not a real problem with the data (the
    _fittable check upstream already confirmed enough examples of each
    class exist overall), just early stopping needing more headroom than
    this particular fold has -- so retry once with it off instead of
    losing the whole cascade to a fold this small.
    """
    kwargs = {**BASE_MODEL_KWARGS, **(params or {})}
    model = HistGradientBoostingClassifier(**kwargs)
    try:
        model.fit(x, y)
    except ValueError:
        kwargs["early_stopping"] = False
        model = HistGradientBoostingClassifier(**kwargs)
        model.fit(x, y)
    return model


def _tune_model(x: np.ndarray, y: list[Any]) -> dict[str, Any]:
    """Light randomized hyperparameter search over PARAM_GRID, scored on log-loss.

    Falls back to {} (library defaults, same as "not enough data to tune"
    below) if the search itself can't run -- see _fit_model's docstring for
    why a small nested-CV fold can trip this even when _fittable passed.
    """
    counts = Counter(y)
    min_class = min(counts.values()) if counts else 0
    if min_class < MIN_SAMPLES_PER_CLASS or len(x) < MIN_SAMPLES_FOR_TUNING:
        return {}
    folds = min(5, min_class)
    total_combos = math.prod(len(v) for v in PARAM_GRID.values())
    search = RandomizedSearchCV(
        HistGradientBoostingClassifier(**BASE_MODEL_KWARGS),
        PARAM_GRID,
        n_iter=min(N_TUNE_ITER, total_combos),
        cv=StratifiedKFold(n_splits=folds, shuffle=True, random_state=0),
        scoring="neg_log_loss",
        random_state=0,
    )
    try:
        search.fit(x, y)
    except ValueError:
        return {}
    return dict(search.best_params_)


def _holdout_accuracy(
    x: np.ndarray, y: list[Any], params: dict[str, Any]
) -> float | None:
    """Cheap single train/holdout accuracy for one model on its own, not the cascade."""
    counts = Counter(y)
    too_few_per_class = min(counts.values(), default=0) < MIN_SAMPLES_PER_CLASS
    if too_few_per_class or len(x) < MIN_SAMPLES_FOR_TUNING:
        return None
    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=0.2, stratify=y, random_state=0
    )
    if len(set(y_train)) < MIN_SAMPLES_PER_CLASS:
        return None
    model = _fit_model(x_train, y_train, params)
    preds = model.predict(x_test)
    return float(sum(p == t for p, t in zip(preds, y_test, strict=True)) / len(y_test))


MIN_EXAMPLES_PER_OUTER_FOLD = 5


def _nested_cascade_cv_accuracy(examples: list[LabeledExample]) -> float | None:
    """Honest nested CV of the full 3-model cascade chained together.

    Correct only if the validity call is right, AND (when valid) the
    completeness call is also exactly right, OR (when not valid) the reason
    call is also exactly right. Hyperparameters are searched fresh inside
    each outer fold on that fold's training rows only -- see
    tools/trip_labeler/app.py's _nested_pipeline_cv_accuracy for the full
    rationale (unchanged here, just extended to a 3-way cascade instead of 2).
    """
    y_valid = [e.is_valid for e in examples]
    counts = Counter(y_valid)
    if not _fittable(y_valid):
        return None
    outer_folds = min(5, *counts.values())
    if len(examples) < outer_folds * MIN_EXAMPLES_PER_OUTER_FOLD:
        return None
    skf = StratifiedKFold(n_splits=outer_folds, shuffle=True, random_state=0)
    x_all = [e.features for e in examples]
    correct = 0
    total = 0
    for train_idx, test_idx in skf.split(x_all, y_valid):
        train_ex = [examples[i] for i in train_idx]
        test_ex = [examples[i] for i in test_idx]

        vx, vy = _valid_xy(train_ex)
        vmodel = _fit_model(vx, vy, _tune_model(vx, vy))

        cx, cy = _completeness_xy(train_ex)
        cmodel = _fit_model(cx, cy, _tune_model(cx, cy)) if _fittable(cy) else None

        rx, ry = _reason_xy(train_ex)
        rmodel = _fit_model(rx, ry, _tune_model(rx, ry)) if _fittable(ry) else None

        for e in test_ex:
            row = _to_x([e.features])
            pred_valid = int(vmodel.predict(row)[0])
            if pred_valid == 1:
                predicted = cmodel.predict(row)[0] if cmodel is not None else None
            else:
                predicted = rmodel.predict(row)[0] if rmodel is not None else None
            actual = e.completeness_label if e.is_valid else e.reason_label
            correct += pred_valid == e.is_valid and predicted == actual
            total += 1
    return correct / total if total else None


def _multiclass_summary(
    y_true: list[str], y_pred: list[str], labels: tuple[str, ...]
) -> dict[str, Any] | None:
    if not y_true:
        return None
    precision, recall, f1, support = precision_recall_fscore_support(
        y_true, y_pred, labels=list(labels), zero_division=0
    )
    correct = sum(t == p for t, p in zip(y_true, y_pred, strict=True))
    return {
        "n": len(y_true),
        "accuracy": correct / len(y_true),
        "per_class": {
            label: {
                "precision": float(precision[i]),
                "recall": float(recall[i]),
                "f1": float(f1[i]),
                "support": int(support[i]),
            }
            for i, label in enumerate(labels)
        },
        "confusion_matrix": confusion_matrix(
            y_true, y_pred, labels=list(labels)
        ).tolist(),
    }


def _classification_metrics(examples: list[LabeledExample]) -> dict[str, Any]:
    """Honest out-of-fold precision/recall/F1/accuracy for all three models."""
    y_valid = [e.is_valid for e in examples]
    counts = Counter(y_valid)
    if not _fittable(y_valid):
        return {"error": "not enough examples of each validity class yet"}
    outer_folds = min(5, *counts.values())
    if len(examples) < outer_folds * MIN_EXAMPLES_PER_OUTER_FOLD:
        return {"error": "not enough examples yet for a stable estimate"}

    skf = StratifiedKFold(n_splits=outer_folds, shuffle=True, random_state=0)
    x_all = [e.features for e in examples]
    valid_true: list[int] = []
    valid_pred: list[int] = []
    comp_true: list[str] = []
    comp_pred: list[str] = []
    reason_true: list[str] = []
    reason_pred: list[str] = []

    for train_idx, test_idx in skf.split(x_all, y_valid):
        train_ex = [examples[i] for i in train_idx]
        test_ex = [examples[i] for i in test_idx]

        vx, vy = _valid_xy(train_ex)
        vmodel = _fit_model(vx, vy, _tune_model(vx, vy))

        cx, cy = _completeness_xy(train_ex)
        cmodel = _fit_model(cx, cy, _tune_model(cx, cy)) if _fittable(cy) else None

        rx, ry = _reason_xy(train_ex)
        rmodel = _fit_model(rx, ry, _tune_model(rx, ry)) if _fittable(ry) else None

        for e in test_ex:
            row = _to_x([e.features])
            pv = int(vmodel.predict(row)[0])
            valid_true.append(e.is_valid)
            valid_pred.append(pv)
            if e.completeness_label is not None and cmodel is not None:
                comp_true.append(e.completeness_label)
                comp_pred.append(cmodel.predict(row)[0])
            if e.reason_label is not None and rmodel is not None:
                reason_true.append(e.reason_label)
                reason_pred.append(rmodel.predict(row)[0])

    return {
        "n_examples": len(examples),
        "n_outer_folds": outer_folds,
        "validity": _multiclass_summary(
            [str(v) for v in valid_true], [str(v) for v in valid_pred], ("0", "1")
        ),
        "completeness": _multiclass_summary(comp_true, comp_pred, MATCH_DECISIONS),
        "reason": _multiclass_summary(reason_true, reason_pred, REASON_DECISIONS),
    }


class LabelIn(BaseModel):
    """POST body for submitting a labeling decision on the current candidate."""

    vehicle_number: str
    line_number: str
    trip_opened_at: datetime
    trip_closed_at: datetime
    candidate_vehicle_id: int
    decision: Decision


class DataStore:
    """Loads/refreshes the candidate pool from data/batches/*.parquet incrementally."""

    def __init__(self) -> None:
        """Load whatever batch files already exist."""
        self._loaded_files: set[str] = set()
        self.df: pl.DataFrame = pl.DataFrame()
        self.refresh()

    def refresh(self) -> bool:
        """Load batch files new since the last call. True if anything was loaded."""
        files = {str(p) for p in BATCHES_DIR.glob("*.parquet")}
        new_files = files - self._loaded_files
        if not new_files:
            return False
        new_df = pl.read_parquet(sorted(new_files))
        self.df = pl.concat([self.df, new_df]) if len(self.df) else new_df
        self._loaded_files |= new_files
        return True


def _feature_matrix(df: pl.DataFrame) -> np.ndarray:
    return df.select(FEATURE_COLUMNS).to_numpy().astype(np.float64)


def _take_unplaced(
    ordering: list[int], placed: set[int], limit: int | None = None
) -> list[int]:
    """Items from `ordering` not yet in `placed`, adding them to `placed` as taken."""
    picked: list[int] = []
    for i in ordering:
        if limit is not None and len(picked) >= limit:
            break
        if i not in placed:
            picked.append(i)
            placed.add(i)
    return picked


def _interleave_tracks(orderings: list[list[int]], n: int) -> list[int]:
    """Round-robin-merge several rankings of range(n) into one deduplicated list.

    Each ordering gets an equal-sized slice of its own top picks first (so
    e.g. a "random" track actually contributes ~n/len(orderings) genuinely
    random items, not just whatever's left over after other tracks claim
    everything), interleaved one-from-each-track. A flat "all of track 1,
    then all of track 2, ..." list would bury later tracks behind however
    long track 1's queue is; interleaving guarantees every track is seen
    regularly rather than one monopolizing the front of a long pool.
    """
    share = max(1, n // len(orderings))
    placed: set[int] = set()
    slices = [_take_unplaced(ordering, placed, share) for ordering in orderings]

    interleaved: list[int] = []
    for group in zip(*slices, strict=False):
        interleaved.extend(group)

    # anything left over (past each track's initial share, or simply never
    # ranked highly by any track) rounds out the pool in each track's own
    # order, then plain index order for whatever's still unplaced
    for ordering in orderings:
        interleaved.extend(_take_unplaced(ordering, placed))
    interleaved.extend(_take_unplaced(list(range(n)), placed))
    return interleaved


def _load_old_validity_model() -> HistGradientBoostingClassifier | None:
    """Load trip_labeler's trained validity model, if one exists. Read-only."""
    if not OLD_VALIDITY_MODEL_PATH.exists():
        return None
    try:
        return joblib.load(OLD_VALIDITY_MODEL_PATH)
    except Exception:  # noqa: BLE001 - any load failure just means: no pre-score
        return None


def _old_model_view(df: pl.DataFrame, direction: str) -> np.ndarray:
    """Build trip_labeler's own feature layout from one direction's columns.

    `gap` mirrors "distance to the runner-up candidate" using the OTHER
    direction's avg_dist_to_line_m, the closest available analog now that
    the two directions of the same trip's own line are the only other
    candidate this model ever saw.
    """
    other = "volta" if direction == "ida" else "ida"
    cols = {
        "avg_dist_to_line_m": f"{direction}_avg_dist_to_line_m",
        "progress_corr": f"{direction}_progress_corr",
        "start_proximity_m": f"{direction}_start_proximity_m",
        "end_proximity_m": f"{direction}_end_proximity_m",
        "speed_percentile": f"{direction}_speed_percentile",
        "implied_speed_kmh": f"{direction}_implied_speed_kmh",
        "shape_start_end_dist_m": f"{direction}_shape_start_end_dist_m",
    }
    return np.column_stack(
        [
            df[cols["avg_dist_to_line_m"]].to_numpy(),
            df[cols["progress_corr"]].to_numpy(),
            df[cols["start_proximity_m"]].to_numpy(),
            df[cols["end_proximity_m"]].to_numpy(),
            df[cols["speed_percentile"]].to_numpy(),
            df[cols["implied_speed_kmh"]].to_numpy(),
            df["n_pings_in_window"].to_numpy(),
            df[f"{other}_avg_dist_to_line_m"].to_numpy()
            - df[f"{direction}_avg_dist_to_line_m"].to_numpy(),
            df["iv_overlap_m"].to_numpy(),
            df[cols["shape_start_end_dist_m"]].to_numpy(),
        ]
    ).astype(np.float64)


def old_model_pre_score(
    df: pl.DataFrame, old_model: HistGradientBoostingClassifier | None
) -> np.ndarray:
    """Best-of-both-directions P(valid) from trip_labeler's model, else all-NaN."""
    if old_model is None or len(df) == 0:
        return np.full(len(df), np.nan)
    p_ida = old_model.predict_proba(_old_model_view(df, "ida"))[:, 1]
    p_volta = old_model.predict_proba(_old_model_view(df, "volta"))[:, 1]
    return np.maximum(p_ida, p_volta)


class LabelStore:
    """Holds the DB connection, candidate pool, and the three active-learning models."""

    def __init__(self, dsn: str) -> None:
        """Connect, ensure schema, load data, restore/train models, fill the pool."""
        self.conn = psycopg.connect(dsn, autocommit=True)
        self._ensure_schema()
        self.lock = threading.Lock()
        self.data = DataStore()
        self.old_model = _load_old_validity_model()
        self._coverage_cache: dict[str, Any] = {
            "n_trips_loaded": 0,
            "n_confident": 0,
            "coverage_pct": None,
        }
        self.pool: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.examples: list[LabeledExample] = []
        self.validity_model: HistGradientBoostingClassifier | None = None
        self.completeness_model: HistGradientBoostingClassifier | None = None
        self.reason_model: HistGradientBoostingClassifier | None = None
        self.validity_params: dict[str, Any] = {}
        self.completeness_params: dict[str, Any] = {}
        self.reason_params: dict[str, Any] = {}
        self.cv_accuracy: float | None = None
        self.cv_accuracy_n: int | None = None
        self.validity_holdout_accuracy: float | None = None
        self.completeness_holdout_accuracy: float | None = None
        self.reason_holdout_accuracy: float | None = None
        self.holdout_n: int | None = None
        self.n_labels = self._count_labels()
        self._labeled_keys: set[tuple[Any, ...]] = set()
        self._load_training_history()
        self._restore_or_train()
        self._refill()

    def _ensure_schema(self) -> None:
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS scratch.trip_finder_labels (
                id bigserial PRIMARY KEY,
                vehicle_number text NOT NULL,
                line_number text NOT NULL,
                trip_opened_at timestamptz NOT NULL,
                trip_closed_at timestamptz NOT NULL,
                candidate_vehicle_id integer NOT NULL,
                decision text NOT NULL CHECK (
                    decision IN ({",".join(f"'{d}'" for d in ALL_DECISIONS)})
                ),
                labeled_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (
                    vehicle_number, line_number, trip_opened_at, trip_closed_at,
                    candidate_vehicle_id
                )
            )
            """
        )

    def _count_labels(self) -> int:
        row = self.conn.execute(
            "SELECT count(*) FROM scratch.trip_finder_labels"
        ).fetchone()
        return row[0] if row else 0

    def _load_training_history(self) -> None:
        """Rebuild self.examples from scratch.trip_finder_labels on startup."""
        rows = self.conn.execute(
            """
            SELECT vehicle_number, line_number, trip_opened_at, trip_closed_at,
                   candidate_vehicle_id, decision
            FROM scratch.trip_finder_labels
            """
        ).fetchall()
        if not rows:
            return
        labels_df = pl.DataFrame(
            rows,
            schema={
                "vehicle_number": pl.Utf8,
                "line_number": pl.Utf8,
                "trip_opened_at": pl.Datetime("us", "UTC"),
                "trip_closed_at": pl.Datetime("us", "UTC"),
                "candidate_vehicle_id": pl.Int64,
                "decision": pl.Utf8,
            },
            orient="row",
        )
        joined = labels_df.join(self.data.df, on=NATURAL_KEY_COLUMNS, how="inner")
        if len(joined) < len(labels_df):
            missing = len(labels_df) - len(joined)
            print(
                f"  {missing} labels reference rows not in the loaded batches, skipped"
            )
        x = _feature_matrix(joined)
        for i, decision in enumerate(joined["decision"].to_list()):
            self.examples.append(_build_example(list(x[i]), decision))
        self._labeled_keys = set(labels_df.select(NATURAL_KEY_COLUMNS).rows())

    def _restore_or_train(self) -> None:
        if self._try_load_saved_models():
            return
        self._train_and_tune(tune=True)

    def _try_load_saved_models(self) -> bool:
        meta_path = MODEL_DIR / "validity_meta.json"
        validity_path = MODEL_DIR / "validity_model.joblib"
        if not meta_path.exists() or not validity_path.exists():
            return False
        try:
            meta = json.loads(meta_path.read_text())
            self.validity_params = meta.get("validity_params", {})
            self.completeness_params = meta.get("completeness_params", {})
            self.reason_params = meta.get("reason_params", {})
            self.validity_model = joblib.load(validity_path)
            completeness_path = MODEL_DIR / "completeness_model.joblib"
            self.completeness_model = (
                joblib.load(completeness_path) if completeness_path.exists() else None
            )
            reason_path = MODEL_DIR / "reason_model.joblib"
            self.reason_model = (
                joblib.load(reason_path) if reason_path.exists() else None
            )
        except Exception:  # noqa: BLE001 - any load failure just means: retrain
            return False
        self.cv_accuracy = meta.get("cv_accuracy")
        self.cv_accuracy_n = meta.get("cv_accuracy_n")
        if meta.get("n_examples", -1) != len(self.examples):
            self._train_and_tune(tune=False)
        else:
            self.validity_holdout_accuracy = meta.get("validity_holdout_accuracy")
            self.completeness_holdout_accuracy = meta.get(
                "completeness_holdout_accuracy"
            )
            self.reason_holdout_accuracy = meta.get("reason_holdout_accuracy")
            self.holdout_n = meta.get("holdout_n")
        return True

    def _train_and_tune(self, *, tune: bool) -> None:
        """Refit the deployed cascade; only retune hyperparameters when `tune`."""
        if len(self.examples) < MIN_LABELS_FOR_MODEL:
            return
        vx, vy = _valid_xy(self.examples)
        if not _fittable(vy):
            return
        if tune:
            self.validity_params = _tune_model(vx, vy)
            cx, cy = _completeness_xy(self.examples)
            if _fittable(cy):
                self.completeness_params = _tune_model(cx, cy)
            rx, ry = _reason_xy(self.examples)
            if _fittable(ry):
                self.reason_params = _tune_model(rx, ry)
            self.cv_accuracy = _nested_cascade_cv_accuracy(self.examples)
            self.cv_accuracy_n = len(self.examples)
        self.validity_model = _fit_model(vx, vy, self.validity_params)
        cx, cy = _completeness_xy(self.examples)
        self.completeness_model = (
            _fit_model(cx, cy, self.completeness_params) if _fittable(cy) else None
        )
        rx, ry = _reason_xy(self.examples)
        self.reason_model = (
            _fit_model(rx, ry, self.reason_params) if _fittable(ry) else None
        )

        self.validity_holdout_accuracy = _holdout_accuracy(vx, vy, self.validity_params)
        self.completeness_holdout_accuracy = (
            _holdout_accuracy(cx, cy, self.completeness_params)
            if _fittable(cy)
            else None
        )
        self.reason_holdout_accuracy = (
            _holdout_accuracy(rx, ry, self.reason_params) if _fittable(ry) else None
        )
        self.holdout_n = len(self.examples)
        self._save_models()

    def _save_models(self) -> None:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        if self.validity_model is not None:
            joblib.dump(self.validity_model, MODEL_DIR / "validity_model.joblib")
        if self.completeness_model is not None:
            joblib.dump(
                self.completeness_model, MODEL_DIR / "completeness_model.joblib"
            )
        if self.reason_model is not None:
            joblib.dump(self.reason_model, MODEL_DIR / "reason_model.joblib")
        meta = {
            "n_examples": len(self.examples),
            "validity_params": self.validity_params,
            "completeness_params": self.completeness_params,
            "reason_params": self.reason_params,
            "cv_accuracy": self.cv_accuracy,
            "cv_accuracy_n": self.cv_accuracy_n,
            "validity_holdout_accuracy": self.validity_holdout_accuracy,
            "completeness_holdout_accuracy": self.completeness_holdout_accuracy,
            "reason_holdout_accuracy": self.reason_holdout_accuracy,
            "holdout_n": self.holdout_n,
            "saved_at": datetime.now(UTC).isoformat(),
        }
        (MODEL_DIR / "validity_meta.json").write_text(json.dumps(meta, indent=2))

    def _maybe_retrain(self) -> bool:
        n = len(self.examples)
        if n < MIN_LABELS_FOR_MODEL:
            return False
        vy = [e.is_valid for e in self.examples]
        if not _fittable(vy):
            return False
        already_trained = self.validity_model is not None
        due = not already_trained or n % RETRAIN_EVERY == 0
        if not due:
            return False
        tune = not already_trained or n % TUNE_EVERY == 0
        self._train_and_tune(tune=tune)
        return True

    def _n_sparse_trips(self) -> int:
        """Count trips where every candidate is sparser than MIN_PINGS_FOR_LABELING.

        These are auto-resolved (confidently INVALID -- there's essentially no
        GPS signal to judge from) without ever needing a model or a human.
        """
        df = self.data.df
        if len(df) == 0:
            return 0
        per_trip_max = df.group_by(NATURAL_KEY_COLUMNS[:4]).agg(
            pl.col("n_pings_in_window").max().alias("max_pings")
        )
        return int((per_trip_max["max_pings"] < MIN_PINGS_FOR_LABELING).sum())

    def _qualifying(self) -> pl.DataFrame:
        """Return loaded candidates with at least MIN_PINGS_FOR_LABELING pings."""
        df = self.data.df
        if len(df) == 0:
            return df
        return df.filter(pl.col("n_pings_in_window") >= MIN_PINGS_FOR_LABELING)

    def _rank_score(self, qualifying: pl.DataFrame) -> np.ndarray:
        """Score every row, descending = more promising (more likely a MATCH).

        This app's own validity model once trained (rescored fresh every
        call, never cached); otherwise trip_labeler's validity model as a
        pre-score (see OLD_VALIDITY_MODEL_PATH); otherwise a naive
        nearest-line-distance heuristic as a last resort. Shared by
        _best_per_trip (coverage) and _top_k_per_trip (labeling pool).
        """
        if self.validity_model is not None:
            return self.validity_model.predict_proba(_feature_matrix(qualifying))[:, 1]
        if self.old_model is not None:
            return old_model_pre_score(qualifying, self.old_model)
        naive = pl.min_horizontal(
            pl.col("ida_avg_dist_to_line_m").fill_null(math.inf),
            pl.col("volta_avg_dist_to_line_m").fill_null(math.inf),
        )
        return -qualifying.select(naive.alias("s"))["s"].to_numpy()

    def _best_per_trip(self) -> pl.DataFrame:
        """Collapse to one row per trip: its current best candidate. Coverage-only.

        NOT the labeling pool source -- see _top_k_per_trip and TOP_K_PER_TRIP
        for why showing only rank 1 per trip starves the validity model of
        negative examples.
        """
        qualifying = self._qualifying()
        if len(qualifying) == 0:
            return qualifying
        scored = qualifying.with_columns(
            pl.Series("_sort_score", self._rank_score(qualifying))
        )
        return (
            scored.sort("_sort_score", descending=True)
            .group_by(NATURAL_KEY_COLUMNS[:4], maintain_order=True)
            .first()
        )

    def _top_k_per_trip(self, k: int) -> pl.DataFrame:
        """Each trip's top k candidates by current score -- the labeling pool source.

        Ranks 2..k are the informative "plausible but wrong" contrast set:
        exactly what the validity model needs to ever see class 0 at all,
        as opposed to random noise from the hundreds of obviously-terrible
        candidates per trip.
        """
        qualifying = self._qualifying()
        if len(qualifying) == 0:
            return qualifying
        scored = qualifying.with_columns(
            pl.Series("_sort_score", self._rank_score(qualifying))
        )
        return (
            scored.sort("_sort_score", descending=True)
            .group_by(NATURAL_KEY_COLUMNS[:4], maintain_order=True)
            .head(k)
        )

    def _score_rows(self, df: pl.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """Return (uncertainty, p_valid) arrays for every row in df."""
        if len(df) == 0 or self.validity_model is None:
            return np.zeros(len(df)), np.full(len(df), 0.5)
        x = _feature_matrix(df)
        p_valid = self.validity_model.predict_proba(x)[:, 1]
        uncertainty = np.abs(p_valid - 0.5)
        if self.completeness_model is not None:
            valid_mask = p_valid >= PROBABILITY_THRESHOLD
            if valid_mask.any():
                proba = self.completeness_model.predict_proba(x[valid_mask])
                margin = 1 - proba.max(
                    axis=1
                )  # 0 = certain, up to 1 = maximally unsure
                uncertainty[valid_mask] = np.minimum(uncertainty[valid_mask], margin)
        if self.reason_model is not None:
            invalid_mask = p_valid < PROBABILITY_THRESHOLD
            if invalid_mask.any():
                p_reason = self.reason_model.predict_proba(x[invalid_mask])[:, 1]
                uncertainty[invalid_mask] = np.minimum(
                    uncertainty[invalid_mask], np.abs(p_reason - 0.5)
                )
        return uncertainty, p_valid

    def _rarest_class_score(self, df: pl.DataFrame, p_valid: np.ndarray) -> np.ndarray:
        """P(whichever of the 7 decision classes has been labeled least so far).

        Combines the validity split with the relevant sub-model via the
        chain rule (P(class) = P(valid) * P(class | valid), or the mirror
        for a reason class) so this always answers "how likely is THIS
        specific rare class", not just "how likely is MATCH" -- otherwise,
        once completeness/reason models exist, the 5-way MATCH split (or the
        2-way reason split) could still starve on whichever of ITS classes is
        rarest even while MATCH-vs-not looks perfectly balanced.

        If the rarest class has ZERO labels so far, it won't even be in the
        relevant sub-model's classes_ (sklearn only knows classes it's
        actually seen) -- there is no learned signal for it at all yet, so
        the honest fallback is random exploration, not silently collapsing
        into P(valid)/P(invalid) (which just duplicates the "promising"
        signal the other tracks already cover and was the actual bug behind
        this track feeling repetitive: BOTH had 0 labels, so this always
        fell through to plain P(valid) instead of ever seeking BOTH out).
        """
        decisions = (e.completeness_label or e.reason_label for e in self.examples)
        decision_counts = Counter(decisions)
        rarest = min(ALL_DECISIONS, key=lambda d: decision_counts.get(d, 0))
        x = _feature_matrix(df)
        if rarest in MATCH_DECISIONS and self.completeness_model is not None:
            classes = list(self.completeness_model.classes_)
            if rarest in classes:
                p_class = self.completeness_model.predict_proba(x)[
                    :, classes.index(rarest)
                ]
                return p_valid * p_class
        elif rarest not in MATCH_DECISIONS and self.reason_model is not None:
            classes = list(self.reason_model.classes_)
            if rarest in classes:
                p_class = self.reason_model.predict_proba(x)[:, classes.index(rarest)]
                return (1 - p_valid) * p_class
        return np.random.default_rng().random(len(df))

    def _refill(self) -> None:
        """Rebuild the pool from each trip's top-K candidates (see TOP_K_PER_TRIP).

        Cold start (no validity model): shuffled randomly, same spirit as
        trip_labeler's own cold-start sampling -- a genuine mix of
        probably-right and probably-wrong candidates in no particular order,
        not "best first", so early labeling sessions build up BOTH classes
        instead of a long run of confirmations (which is exactly what was
        happening before this method existed: every label came out MATCH).

        Once the validity model exists, interleaves three tracks so the
        model actually learns to predict well everywhere, not just at the
        boundary it already knows about:
          - random: a genuinely unbiased sample, so predictions stay
            calibrated across the whole feature space, not just near
            whatever edge cases the other two tracks keep circling.
          - uncertain: closest to a coin flip across the whole cascade
            (validity, then completeness or reason as applicable) -- refines
            the decision boundary.
          - rarest class (see _rarest_class_score): forces continued
            exposure to whichever of the 7 decision classes has the fewest
            labels so far, regardless of how confident the model already is
            about it -- pure uncertainty sampling alone won't reliably keep
            surfacing a class the model has already learned to dismiss.
        """
        self.data.refresh()
        self._recompute_coverage()
        pool_df = self._top_k_per_trip(TOP_K_PER_TRIP)
        if len(pool_df) == 0:
            return
        labeled = self._labeled_keys
        if labeled:
            key_exprs = [pl.col(c) for c in NATURAL_KEY_COLUMNS]
            mask = pl.struct(key_exprs).map_elements(
                lambda s: tuple(s.values()) not in labeled, return_dtype=pl.Boolean
            )
            pool_df = pool_df.filter(mask)
        if len(pool_df) == 0:
            return

        if self.validity_model is None:
            order = np.random.default_rng().permutation(len(pool_df)).tolist()
            self.pool = [pool_df.row(i, named=True) for i in order]
            return

        uncertainty, p_valid = self._score_rows(pool_df)
        rarest_score = self._rarest_class_score(pool_df, p_valid)
        n = len(pool_df)
        random_order = np.random.default_rng().permutation(n).tolist()
        uncertain_order = [int(i) for i in np.argsort(uncertainty)]
        rarest_order = [int(i) for i in np.argsort(-rarest_score)]
        order = _interleave_tracks([random_order, uncertain_order, rarest_order], n)
        self.pool = [pool_df.row(i, named=True) for i in order]

    def next_candidate(self) -> dict[str, Any]:
        """Pop the most useful (trip, candidate) pair to label next."""
        with self.lock:
            if len(self.pool) < POOL_REFILL_AT:
                self._refill()
            if not self.pool:
                detail = (
                    "pool exhausted, nothing left to label (or no batches loaded yet)"
                )
                raise HTTPException(status_code=404, detail=detail)
            self.current = self.pool.pop(0)
            return self.current

    def submit_label(self, payload: LabelIn) -> None:
        """Persist a decision, fold it into the training set, and retrain if ready."""
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO scratch.trip_finder_labels
                    (vehicle_number, line_number, trip_opened_at, trip_closed_at,
                     candidate_vehicle_id, decision)
                VALUES (%(vehicle_number)s, %(line_number)s,
                        %(trip_opened_at)s, %(trip_closed_at)s,
                        %(candidate_vehicle_id)s, %(decision)s)
                ON CONFLICT (
                    vehicle_number, line_number, trip_opened_at, trip_closed_at,
                    candidate_vehicle_id
                ) DO UPDATE SET decision = EXCLUDED.decision, labeled_at = now()
                """,
                payload.model_dump(),
            )
            self.n_labels += 1
            key = tuple(payload.model_dump()[c] for c in NATURAL_KEY_COLUMNS)
            self._labeled_keys.add(key)

            if (
                self.current is not None
                and tuple(self.current[c] for c in NATURAL_KEY_COLUMNS) == key
            ):
                x = list(_feature_matrix(pl.DataFrame([self.current]))[0])
                self.examples.append(_build_example(x, payload.decision))
                self.current = None

            if self._maybe_retrain():
                self._refill()

    def coverage(self) -> dict[str, Any]:
        """Return the cached coverage stat -- see _recompute_coverage for the real work.

        This is embedded in the JSON response of every /api/next and
        /api/label call, so it must be cheap: _recompute_coverage does a
        full rescore of the entire loaded dataset (10M+ rows and growing),
        which took 4-6 seconds per call once run on every single request --
        that's the whole reason this is a cache instead of a live call.
        """
        return self._coverage_cache

    def _recompute_coverage(self) -> None:
        """Do the actual expensive coverage computation and cache it.

        Called from _refill (startup, every pool-empties-below-POOL_REFILL_AT
        refill, and every retrain-triggered refill -- see submit_label) --
        not on every request. % of currently-loaded trips confidently
        resolved -- the stopping criterion.

        A trip counts as resolved if EITHER every one of its candidates is
        too sparse to mean anything (auto-INVALID, see _n_sparse_trips) OR
        this app's own validity model has reached high/low confidence on its
        best qualifying candidate. Deliberately the same "confident either
        way" definition trip_labeler/predict_trips.py uses for its own
        good_prediction/not-good split, so this number means the same thing.
        """
        df = self.data.df
        if len(df) == 0:
            self._coverage_cache = {
                "n_trips_loaded": 0,
                "n_confident": 0,
                "coverage_pct": None,
            }
            return
        n_trips = df.select(NATURAL_KEY_COLUMNS[:4]).unique().height
        n_sparse = self._n_sparse_trips()
        if self.validity_model is None:
            self._coverage_cache = {
                "n_trips_loaded": n_trips,
                "n_confident": n_sparse,
                "coverage_pct": round(100 * n_sparse / n_trips, 2) if n_trips else None,
            }
            return
        best = self._best_per_trip()
        _, p_valid = self._score_rows(best)
        n_model_confident = int(
            ((p_valid >= VALID_HIGH_CONF) | (p_valid <= VALID_LOW_CONF)).sum()
        )
        n_confident = n_sparse + n_model_confident
        self._coverage_cache = {
            "n_trips_loaded": n_trips,
            "n_confident": n_confident,
            "coverage_pct": round(100 * n_confident / n_trips, 2),
        }

    def stats(self) -> dict[str, Any]:
        """Return current progress counters for the UI header."""
        return {
            "n_labels": self.n_labels,
            "pool_size": len(self.pool),
            "n_trips_total_target": 37515,
            "n_batches_loaded": len(self.data._loaded_files),  # noqa: SLF001
            "validity_trained": self.validity_model is not None,
            "completeness_trained": self.completeness_model is not None,
            "reason_trained": self.reason_model is not None,
            "n_training_examples": len(self.examples),
            "cv_accuracy": self.cv_accuracy,
            "cv_accuracy_n": self.cv_accuracy_n,
            "validity_holdout_accuracy": self.validity_holdout_accuracy,
            "completeness_holdout_accuracy": self.completeness_holdout_accuracy,
            "reason_holdout_accuracy": self.reason_holdout_accuracy,
            "holdout_n": self.holdout_n,
            **self.coverage(),
        }


store = LabelStore(DSN)
app = FastAPI()


def _fetch_pings(
    vehicle_id: int, opened_at: datetime, closed_at: datetime
) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        """
        SELECT metric_timestamp, latitude, longitude
        FROM silver.avl_pings
        WHERE vehicle_id = %(vehicle_id)s
          AND metric_timestamp BETWEEN %(opened_at)s AND %(closed_at)s
        ORDER BY metric_timestamp
        """,
        {"vehicle_id": vehicle_id, "opened_at": opened_at, "closed_at": closed_at},
    ).fetchall()
    return [{"t": r[0].isoformat(), "lat": r[1], "lon": r[2]} for r in rows]


def _fetch_shapes(
    feed_version_date: date, line_number: str
) -> dict[str, list[tuple[float, float]]]:
    rows = store.conn.execute(
        """
        SELECT shape_id, ST_AsGeoJSON(line_geom)
        FROM scratch.route_shape_geoms
        WHERE feed_version_date = %(feed)s AND line_number = %(line)s
        """,
        {"feed": feed_version_date, "line": line_number},
    ).fetchall()
    out: dict[str, list[tuple[float, float]]] = {}
    for shape_id, geojson in rows:
        coords = json.loads(geojson)["coordinates"]
        out[shape_id] = [(lat, lon) for lon, lat in coords]
    return out


def _candidate_to_json(row: dict[str, Any]) -> dict[str, Any]:
    x = _feature_matrix(pl.DataFrame([row]))

    validity_pred: dict[str, Any] | None = None
    if store.validity_model is not None:
        p_valid = float(store.validity_model.predict_proba(x)[0][1])
        validity_pred = {
            "predicted_valid": p_valid >= PROBABILITY_THRESHOLD,
            "valid_probability": round(p_valid, 4),
        }

    completeness_pred: dict[str, Any] | None = None
    if (
        store.completeness_model is not None
        and validity_pred is not None
        and validity_pred["predicted_valid"]
    ):
        proba = store.completeness_model.predict_proba(x)[0]
        classes = store.completeness_model.classes_
        best_i = int(np.argmax(proba))
        completeness_pred = {
            "predicted_completeness": str(classes[best_i]),
            "predicted_probability": round(float(proba[best_i]), 4),
        }

    reason_pred: dict[str, Any] | None = None
    if (
        store.reason_model is not None
        and validity_pred is not None
        and not validity_pred["predicted_valid"]
    ):
        proba = store.reason_model.predict_proba(x)[0]
        classes = store.reason_model.classes_
        best_i = int(np.argmax(proba))
        reason_pred = {
            "predicted_reason": str(classes[best_i]),
            "predicted_probability": round(float(proba[best_i]), 4),
        }

    pings = _fetch_pings(
        row["candidate_vehicle_id"], row["trip_opened_at"], row["trip_closed_at"]
    )
    shapes = _fetch_shapes(row["resolved_feed_version_date"], row["line_number"])

    return {
        "vehicle_number": row["vehicle_number"],
        "line_number": row["line_number"],
        "trip_opened_at": row["trip_opened_at"].isoformat(),
        "trip_closed_at": row["trip_closed_at"].isoformat(),
        "candidate_vehicle_id": row["candidate_vehicle_id"],
        "resolved_feed_version_date": row["resolved_feed_version_date"].isoformat(),
        "model_prediction": {
            "validity": validity_pred,
            "completeness": completeness_pred,
            "reason": reason_pred,
        },
        "features": {c: _clean(row[c]) for c in FEATURE_COLUMNS},
        "pings": pings,
        "shapes": {
            sid: [{"lat": lat, "lon": lon} for lat, lon in pts]
            for sid, pts in shapes.items()
        },
        "stats": store.stats(),
    }


@app.get("/")
def index() -> FileResponse:
    """Serve the labeling UI."""
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/next")
def api_next() -> JSONResponse:
    """Return the next (trip, candidate) pair to label, ranked by model uncertainty."""
    row = store.next_candidate()
    return JSONResponse(_candidate_to_json(row))


@app.post("/api/label")
def api_label(payload: LabelIn) -> dict[str, Any]:
    """Record a labeling decision for the current candidate."""
    store.submit_label(payload)
    return {"ok": True, "stats": store.stats()}


@app.get("/api/metrics")
def api_metrics() -> dict[str, Any]:
    """Compute and persist full precision/recall/F1 metrics for the cascade."""
    metrics = _classification_metrics(store.examples)
    metrics["computed_at"] = datetime.now(UTC).isoformat()
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    (MODEL_DIR / "metrics.json").write_text(json.dumps(metrics, indent=2))
    return metrics


if __name__ == "__main__":
    # tailnet-only dev box, matches trip_labeler's own exposure pattern
    uvicorn.run(app, host="0.0.0.0", port=8011)  # noqa: S104
