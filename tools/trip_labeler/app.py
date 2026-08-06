"""Active-learning labeling app for AVL-to-GTFS trip match candidates.

Reads candidate matches from scratch.device_mapping_trip_scores /
scratch.vehicle_mapping_trip_scores plus raw pings from silver.avl_pings and
shape geometry from scratch.route_shape_geoms, and writes decisions to a new
scratch.trip_match_labels table (created on first run). Does not touch any
existing table.

Three models share the same 10-feature vector but answer different questions:
  - validity model: is the trip window explained by one of its candidates at
    all? (target: decision == MATCH, trained on every label)
  - candidate model: GIVEN it's valid and there are two candidates, is the
    naive-best one (smallest avg_dist_to_line_m) the right one? (trained only
    on MATCH decisions with two candidates)
  - reason model: GIVEN it's NOT valid, is it NOT_THIS_ROUTE (a real,
    coherent route - just the wrong line) or INVALID_TRIP (no coherent route
    at all)? (trained only on INVALID_TRIP/NOT_THIS_ROUTE decisions)
The headline number in the UI is the cross-validated accuracy of the
validity+candidate models chained together, not either model's own score in
isolation - the reason model answers a separate question and is reported on
its own. All three models and their tuned hyperparameters are persisted to
./model_store/ so labeling progress survives a server restart cheaply (no
need to retune from scratch).

Run with:
    uv run tools/trip_labeler/app.py

Then open http://localhost:8010
"""

from __future__ import annotations

import json
import math
import random
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import joblib
import psycopg
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from scoring import (
    SOURCES,
    TWO_CANDIDATES,
    Candidate,
    best_candidate,
    compute_feature_vector,
    load_iv_overlap,
    load_shape_start_end_dist,
)
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import confusion_matrix, precision_recall_fscore_support
from sklearn.model_selection import (
    RandomizedSearchCV,
    StratifiedKFold,
    train_test_split,
)

DSN = "postgresql://opa:opa@localhost:5432/opa"
MODEL_DIR = Path(__file__).parent / "model_store"

POOL_REFILL_AT = 40
BATCH_FETCH = 250
MIN_LABELS_FOR_MODEL = 20
RETRAIN_EVERY = 10  # after the first fit, only refit every N new labels
TUNE_EVERY = 50  # only re-run hyperparameter search every N new labels
# empirically: p50 avg_dist_to_line_m ~= 14.5m (obviously right), p75 ~= 340m
# (usually obviously wrong). Sample the ambiguous band between those, not the
# extremes, so labeling time isn't spent confirming what a threshold already
# knows. COLD START ONLY - once a validity model exists, SWEEP_SAMPLE_PCT
# below replaces this with real model-uncertainty sampling over the full table.
BOUNDARY_DIST_MIN_M = 8
BOUNDARY_DIST_MAX_M = 800
SWEEP_SAMPLE_PCT = 2  # % of the full table scanned per refill once a model exists

PARAM_GRID: dict[str, list[Any]] = {
    "max_depth": [3, 4, 6, None],
    "learning_rate": [0.05, 0.1, 0.2],
    "max_iter": [50, 100, 200],
    "l2_regularization": [0.0, 0.5, 1.0, 2.0],
}
N_TUNE_ITER = 20
# HistGradientBoostingClassifier's early_stopping default ('auto') disables
# early stopping entirely for n_samples < 10_000, which is every dataset size
# this app will ever see - meaning without forcing it on, the model always
# builds the full max_iter trees regardless of whether it's already overfit.
# Combined with tuning purely for accuracy (which doesn't penalize being
# confidently wrong, only being wrong), that's what was producing near-100%
# probabilities on a genuinely tiny label set. Forcing early_stopping=True
# here, plus scoring the search on log-loss instead of accuracy below, fixes
# both: log-loss is a proper scoring rule that punishes overconfident wrong
# answers, so the search now has a real incentive to prefer calibrated models.
BASE_MODEL_KWARGS: dict[str, Any] = {"early_stopping": True, "random_state": 0}

MIN_SAMPLES_PER_CLASS = 2  # can't fold or split without at least 2 of each class
MIN_SAMPLES_FOR_TUNING = 10  # below this, a hyperparameter search is too noisy
PROBABILITY_THRESHOLD = 0.5  # decision boundary for turning a probability into a call

# UNSURE is recorded (so the window is never resurfaced) but deliberately
# excluded from training everywhere below - it's an escape hatch for the
# labeler, not a class either model ever predicts.
Decision = Literal["MATCH", "INVALID_TRIP", "NOT_THIS_ROUTE", "UNSURE"]


@dataclass
class Window:
    """A single (entity, trip window) up for labeling.

    Carries its candidates, raw pings, and shape geometry.
    """

    source: str
    entity_id: str
    trip_opened_at: datetime
    trip_closed_at: datetime
    resolved_feed_version_date: date
    candidates: list[Candidate] = field(default_factory=list)
    pings: list[tuple[datetime, float, float]] = field(default_factory=list)
    shapes: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    # why this window entered the pool: "uncertain" (near the decision boundary)
    # or "predicted_invalid" (model currently leans invalid, regardless of how
    # confident). _rank_pool interleaves these instead of a single uncertainty
    # sort, or the predicted_invalid half would never surface - see
    # _fetch_worst_confidence for why that half exists at all.
    priority: str = "uncertain"

    @property
    def key(self) -> tuple[str, str, datetime]:
        """Return the (source, entity_id, trip_opened_at) identity of this window."""
        return (self.source, self.entity_id, self.trip_opened_at)

    def best_candidate(self) -> Candidate:
        """Return the candidate with the smallest avg_dist_to_line_m."""
        return best_candidate(self.candidates)

    def feature_vector(
        self,
        iv_overlap_m: dict[tuple[date, str], float],
        shape_start_end_dist_m: dict[tuple[date, str, str], float],
    ) -> list[float]:
        """Build the shared feature vector used by both models.

        Delegates to scoring.compute_feature_vector - see there for the
        actual definition, kept in one place so the live app and any batch
        inference script can never silently diverge.
        """
        return compute_feature_vector(
            self.candidates,
            self.resolved_feed_version_date,
            iv_overlap_m,
            shape_start_end_dist_m,
        )


@dataclass
class LabeledExample:
    """One labeled window, reduced to what all three models need.

    `picked_naive_best` is only meaningful (non-None) when the decision was
    MATCH and there were two candidates - that's the only case where "which
    candidate" was actually a question. It is None for INVALID_TRIP,
    NOT_THIS_ROUTE, and single-candidate MATCH windows alike, and the
    candidate model is trained only on rows where it isn't None.

    `invalid_reason` is only meaningful (non-None) when the decision was
    NOT valid: 1 for NOT_THIS_ROUTE (a real, coherent route - just not the
    assigned line), 0 for INVALID_TRIP (no coherent route at all). None for
    MATCH, where the question doesn't apply. The reason model is trained
    only on rows where it isn't None.
    """

    features: list[float]
    is_valid: int
    is_two_candidate: bool
    picked_naive_best: int | None
    invalid_reason: int | None


def _clean(v: Any) -> Any:  # noqa: ANN401
    """Turn NaN into None so it survives standard JSON serialization."""
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _build_example(
    win: Window,
    decision: str,
    chosen_shape_id: str | None,
    iv_overlap_m: dict[tuple[date, str], float],
    shape_start_end_dist_m: dict[tuple[date, str, str], float],
) -> LabeledExample:
    best = win.best_candidate()
    is_valid = 1 if decision == "MATCH" else 0
    is_two = len(win.candidates) == TWO_CANDIDATES
    picked_naive_best = None
    if is_valid and is_two:
        picked_naive_best = 1 if chosen_shape_id == best.shape_id else 0
    invalid_reason = None
    if not is_valid:
        invalid_reason = 1 if decision == "NOT_THIS_ROUTE" else 0
    return LabeledExample(
        features=win.feature_vector(iv_overlap_m, shape_start_end_dist_m),
        is_valid=is_valid,
        is_two_candidate=is_two,
        picked_naive_best=picked_naive_best,
        invalid_reason=invalid_reason,
    )


def _valid_xy(
    examples: list[LabeledExample],
) -> tuple[list[list[float]], list[int]]:
    return [e.features for e in examples], [e.is_valid for e in examples]


def _candidate_xy(
    examples: list[LabeledExample],
) -> tuple[list[list[float]], list[int]]:
    features: list[list[float]] = []
    labels: list[int] = []
    for e in examples:
        if e.picked_naive_best is not None:
            features.append(e.features)
            labels.append(e.picked_naive_best)
    return features, labels


def _reason_xy(
    examples: list[LabeledExample],
) -> tuple[list[list[float]], list[int]]:
    """Features/labels for the reason model: NOT_THIS_ROUTE (1) vs INVALID_TRIP (0).

    Trained only on the "not valid" subset - see LabeledExample.invalid_reason.
    """
    features: list[list[float]] = []
    labels: list[int] = []
    for e in examples:
        if e.invalid_reason is not None:
            features.append(e.features)
            labels.append(e.invalid_reason)
    return features, labels


def _fit_model(
    x: list[list[float]], y: list[int], params: dict[str, Any] | None = None
) -> HistGradientBoostingClassifier:
    model = HistGradientBoostingClassifier(**BASE_MODEL_KWARGS, **(params or {}))
    model.fit(x, y)
    return model


def _tune_model(x: list[list[float]], y: list[int]) -> dict[str, Any]:
    """Light randomized hyperparameter search over PARAM_GRID.

    Scored on log-loss (not accuracy) so the search is actually rewarded for
    producing calibrated probabilities, not just right/wrong answers. Falls
    back to library defaults (empty dict) when there isn't enough data to fold.
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
    search.fit(x, y)
    return dict(search.best_params_)


def _holdout_accuracy(
    x: list[list[float]], y: list[int], params: dict[str, Any]
) -> float | None:
    """Cheap single train/holdout-split accuracy for ONE model on its own.

    Not the joint pipeline, not nested. Reuses whatever hyperparameters are
    currently deployed rather than searching - one fit, not hundreds - so
    it's fast enough to recompute on every plain refit (every RETRAIN_EVERY
    labels), unlike the nested pipeline metric which only runs every
    TUNE_EVERY labels. This is a quicker, less rigorous pulse-check between
    the expensive recomputations, not a replacement for them: since it reuses
    hyperparameters tuned on the full dataset, it carries the same mild
    optimism the nested version exists to avoid.
    """
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


# below this, an outer fold's own inner hyperparameter search is too noisy to trust
MIN_EXAMPLES_PER_OUTER_FOLD = 5


def _nested_pipeline_cv_accuracy(examples: list[LabeledExample]) -> float | None:
    """Honest nested cross-validation of the two models chained together.

    Correct only if the validity call is right, and - when valid - the
    candidate call is also right too. This is the number shown in the UI.

    Critically, hyperparameters are searched FRESH inside each outer fold,
    using only that fold's training rows - never the fold's own held-out test
    rows. A simpler (and wrong) approach would tune hyperparameters once on
    the full dataset and then "cross-validate" on folds of that same data;
    that leaks the test rows into the search indirectly and reads
    optimistic. This is more expensive (a full hyperparameter search per
    outer fold, not once total) but is the only way the reported number
    actually means what it claims to mean. It is intentionally decoupled
    from the DEPLOYED models' own hyperparameters (see _train_and_tune) -
    those are legitimately tuned on all available data, since more data only
    helps a model you're actually going to use; nested CV exists purely to
    *measure* honestly, not to pick what ships.
    """
    y_valid = [e.is_valid for e in examples]
    counts = Counter(y_valid)
    if min(counts.values(), default=0) < MIN_SAMPLES_PER_CLASS:
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
        v_params = _tune_model(vx, vy)  # inner search: only this fold's own rows
        vmodel = _fit_model(vx, vy, v_params)

        cx, cy = _candidate_xy(train_ex)
        cmodel = None
        if len(set(cy)) > 1:
            c_params = _tune_model(cx, cy)
            cmodel = _fit_model(cx, cy, c_params)

        for e in test_ex:
            pred_valid = int(vmodel.predict([e.features])[0])
            predicted_naive_best: int | None = None
            if pred_valid == 1:
                if not e.is_two_candidate:
                    predicted_naive_best = 1  # only one candidate - trivially "it"
                elif cmodel is not None:
                    predicted_naive_best = int(cmodel.predict([e.features])[0])
                else:
                    predicted_naive_best = 1  # no candidate model yet: fall back

            actual_naive_best = None
            if e.is_valid:
                actual_naive_best = 1 if not e.is_two_candidate else e.picked_naive_best

            correct += (
                pred_valid == e.is_valid and predicted_naive_best == actual_naive_best
            )
            total += 1
    return correct / total if total else None


def _binary_summary(y_true: list[int], y_pred: list[int]) -> dict[str, Any] | None:
    """Precision/recall/F1 per class, overall accuracy, and confusion matrix.

    `confusion_matrix` is [[tn, fp], [fn, tp]] with class 0 first, matching
    sklearn's default label ordering for binary {0, 1} targets.
    """
    if not y_true or len(set(y_true)) < MIN_SAMPLES_PER_CLASS:
        return None
    precision, recall, f1, support = precision_recall_fscore_support(
        y_true, y_pred, labels=[0, 1], zero_division=0
    )
    correct = sum(t == p for t, p in zip(y_true, y_pred, strict=True))
    return {
        "n": len(y_true),
        "accuracy": correct / len(y_true),
        "per_class": {
            str(label): {
                "precision": float(precision[label]),
                "recall": float(recall[label]),
                "f1": float(f1[label]),
                "support": int(support[label]),
            }
            for label in (0, 1)
        },
        "confusion_matrix": confusion_matrix(y_true, y_pred, labels=[0, 1]).tolist(),
    }


def _classification_metrics(examples: list[LabeledExample]) -> dict[str, Any]:
    """Honest out-of-fold precision/recall/F1/accuracy for all three models.

    Same nested methodology as _nested_pipeline_cv_accuracy (fresh
    hyperparameter search per outer fold, never touching that fold's own
    held-out rows) - but instead of collapsing to one joint-pipeline
    accuracy number, this collects every fold's raw predictions and scores
    them once at the end. That's the standard way to report CV
    classification metrics on a small sample: aggregating out-of-fold
    predictions before scoring is more stable than averaging per-fold
    metrics, which on ~5-row folds would be dominated by noise.
    Class 1 = "valid" for the validity model, "the naive-best candidate was
    the right one" for the candidate model, and "NOT_THIS_ROUTE" (vs
    INVALID_TRIP) for the reason model; class 0 is the opposite of each.
    """
    y_valid = [e.is_valid for e in examples]
    counts = Counter(y_valid)
    if min(counts.values(), default=0) < MIN_SAMPLES_PER_CLASS:
        return {"error": "not enough examples of each validity class yet"}
    outer_folds = min(5, *counts.values())
    if len(examples) < outer_folds * MIN_EXAMPLES_PER_OUTER_FOLD:
        return {"error": "not enough examples yet for a stable estimate"}

    skf = StratifiedKFold(n_splits=outer_folds, shuffle=True, random_state=0)
    x_all = [e.features for e in examples]
    valid_true: list[int] = []
    valid_pred: list[int] = []
    cand_true: list[int] = []
    cand_pred: list[int] = []
    reason_true: list[int] = []
    reason_pred: list[int] = []

    for train_idx, test_idx in skf.split(x_all, y_valid):
        train_ex = [examples[i] for i in train_idx]
        test_ex = [examples[i] for i in test_idx]

        vx, vy = _valid_xy(train_ex)
        v_params = _tune_model(vx, vy)
        vmodel = _fit_model(vx, vy, v_params)

        cx, cy = _candidate_xy(train_ex)
        cmodel = None
        if len(set(cy)) > 1:
            c_params = _tune_model(cx, cy)
            cmodel = _fit_model(cx, cy, c_params)

        rx, ry = _reason_xy(train_ex)
        rmodel = None
        if len(set(ry)) > 1:
            r_params = _tune_model(rx, ry)
            rmodel = _fit_model(rx, ry, r_params)

        for e in test_ex:
            pv = int(vmodel.predict([e.features])[0])
            valid_true.append(e.is_valid)
            valid_pred.append(pv)
            if e.picked_naive_best is not None and cmodel is not None:
                cp = int(cmodel.predict([e.features])[0])
                cand_true.append(e.picked_naive_best)
                cand_pred.append(cp)
            if e.invalid_reason is not None and rmodel is not None:
                rp = int(rmodel.predict([e.features])[0])
                reason_true.append(e.invalid_reason)
                reason_pred.append(rp)

    return {
        "n_examples": len(examples),
        "n_outer_folds": outer_folds,
        "validity": _binary_summary(valid_true, valid_pred),
        "candidate": _binary_summary(cand_true, cand_pred),
        "reason": _binary_summary(reason_true, reason_pred),
    }


class LabelIn(BaseModel):
    """POST body for submitting a labeling decision on the current window."""

    source: str
    entity_id: str
    trip_opened_at: datetime
    trip_closed_at: datetime
    line_number: str | None
    resolved_feed_version_date: date | None
    decision: Decision
    chosen_shape_id: str | None = None


class LabelStore:
    """Holds the DB connection, candidate pool, and the two active-learning models.

    Single-process, single-user by design.
    """

    def __init__(self, dsn: str) -> None:
        """Connect, load static geometry lookups, and restore/train the models.

        Args:
            dsn: Postgres connection string for the silver/scratch database.

        """
        self.conn = psycopg.connect(dsn, autocommit=True)
        self._ensure_schema()
        self.lock = threading.Lock()
        self.pool: list[Window] = []
        self.current: Window | None = None
        self.examples: list[LabeledExample] = []
        self.validity_model: HistGradientBoostingClassifier | None = None
        self.candidate_model: HistGradientBoostingClassifier | None = None
        self.reason_model: HistGradientBoostingClassifier | None = None
        self.validity_params: dict[str, Any] = {}
        self.candidate_params: dict[str, Any] = {}
        self.reason_params: dict[str, Any] = {}
        self.cv_accuracy: float | None = None
        self.cv_accuracy_n: int | None = None  # n_labels cv_accuracy was measured on
        self.validity_holdout_accuracy: float | None = None
        self.candidate_holdout_accuracy: float | None = None
        self.reason_holdout_accuracy: float | None = None
        self.holdout_n: int | None = None  # n_labels the holdout numbers reflect
        self.n_labels = self._count_labels()
        self._source_cycle = ["device", "vehicle"]
        self.iv_overlap_m = load_iv_overlap(self.conn)
        self.shape_start_end_dist_m = load_shape_start_end_dist(self.conn)
        self._load_training_history()
        self._restore_or_train()
        self._refill()

    def _load_training_history(self) -> None:
        """Rebuild self.examples from scratch.trip_match_labels on startup.

        Labels themselves always survive a restart (they're in Postgres); this
        re-derives the feature vectors from the same candidate tables so a
        restart doesn't lose labeling progress even if the saved model files
        are missing or stale.
        """
        rows = self.conn.execute(
            """
            SELECT source, entity_id, trip_opened_at, trip_closed_at,
                   decision, chosen_shape_id
            FROM scratch.trip_match_labels
            """
        ).fetchall()
        by_source: dict[str, list[tuple[Any, ...]]] = {"device": [], "vehicle": []}
        for r in rows:
            by_source[r[0]].append(r)
        for source, label_rows in by_source.items():
            if not label_rows:
                continue
            keys = [(r[1], r[2], r[3]) for r in label_rows]
            windows_by_key = self._fetch_candidates(source, keys)
            for r in label_rows:
                _src, entity_id, trip_opened_at, _closed, decision, chosen_shape_id = r
                if decision == "UNSURE":
                    continue
                win = windows_by_key.get((entity_id, trip_opened_at))
                if win is None or not win.candidates:
                    # underlying candidate rows can't be found anymore (rare) - skip
                    # rather than crash startup over one stale label
                    continue
                self.examples.append(
                    _build_example(
                        win,
                        decision,
                        chosen_shape_id,
                        self.iv_overlap_m,
                        self.shape_start_end_dist_m,
                    )
                )

    def _restore_or_train(self) -> None:
        """Try to reuse a persisted model first (fast).

        Falls back to a full train (+ hyperparameter search) if nothing
        usable is on disk.
        """
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
            self.candidate_params = meta.get("candidate_params", {})
            self.reason_params = meta.get("reason_params", {})
            self.validity_model = joblib.load(validity_path)
            candidate_path = MODEL_DIR / "candidate_model.joblib"
            self.candidate_model = (
                joblib.load(candidate_path) if candidate_path.exists() else None
            )
            reason_path = MODEL_DIR / "reason_model.joblib"
            self.reason_model = (
                joblib.load(reason_path) if reason_path.exists() else None
            )
        except Exception:  # noqa: BLE001 - any load failure just means: retrain
            return False
        # the nested pipeline estimate only ever gets (re)computed at a real
        # tune event (every TUNE_EVERY) - always restore it from the cache
        # regardless of whether labels moved on since, rather than silently
        # losing a perfectly valid measurement on every restart in between
        self.cv_accuracy = meta.get("cv_accuracy")
        self.cv_accuracy_n = meta.get("cv_accuracy_n")
        if meta.get("n_examples", -1) != len(self.examples):
            # labels moved on since this was saved - reuse the tuned
            # hyperparameters (skip the expensive search) but refit on the
            # current data (this also refreshes the cheap holdout numbers)
            self._train_and_tune(tune=False)
        else:
            self.validity_holdout_accuracy = meta.get("validity_holdout_accuracy")
            self.candidate_holdout_accuracy = meta.get("candidate_holdout_accuracy")
            self.reason_holdout_accuracy = meta.get("reason_holdout_accuracy")
            self.holdout_n = meta.get("holdout_n")
        return True

    def _train_and_tune(self, *, tune: bool) -> None:
        """Refit the DEPLOYED models, cheaply unless `tune` is due.

        Cheap and fast when `tune` is False (reuses the last-known-good
        hyperparameters, every RETRAIN_EVERY labels, so labeling flow never
        stalls). When `tune` is True (every TUNE_EVERY labels) it also
        re-searches hyperparameters on the full dataset AND remeasures
        honest nested-CV accuracy - both are the same order of expense (a
        full search per outer fold vs. one search total), so they're paired
        on the same, deliberately infrequent, cadence. The nested
        measurement is otherwise independent of these hyperparameters
        entirely: it does its own per-fold search from scratch.

        Separately, per-model holdout accuracy is recomputed every call
        (i.e. every RETRAIN_EVERY, not just every TUNE_EVERY) - a single
        train/holdout split per model, cheap enough to run every time, as a
        quicker (less rigorous) pulse-check between the expensive nested
        recomputations.
        """
        if len(self.examples) < MIN_LABELS_FOR_MODEL:
            return
        vx, vy = _valid_xy(self.examples)
        if len(set(vy)) < MIN_SAMPLES_PER_CLASS:
            return
        if tune:
            self.validity_params = _tune_model(vx, vy)
            cx, cy = _candidate_xy(self.examples)
            if len(set(cy)) > 1:
                self.candidate_params = _tune_model(cx, cy)
            rx, ry = _reason_xy(self.examples)
            if len(set(ry)) > 1:
                self.reason_params = _tune_model(rx, ry)
            self.cv_accuracy = _nested_pipeline_cv_accuracy(self.examples)
            self.cv_accuracy_n = len(self.examples)
        self.validity_model = _fit_model(vx, vy, self.validity_params)
        cx, cy = _candidate_xy(self.examples)
        self.candidate_model = (
            _fit_model(cx, cy, self.candidate_params) if len(set(cy)) > 1 else None
        )
        rx, ry = _reason_xy(self.examples)
        self.reason_model = (
            _fit_model(rx, ry, self.reason_params) if len(set(ry)) > 1 else None
        )

        self.validity_holdout_accuracy = _holdout_accuracy(vx, vy, self.validity_params)
        self.candidate_holdout_accuracy = None
        if len(set(cy)) > 1:
            self.candidate_holdout_accuracy = _holdout_accuracy(
                cx, cy, self.candidate_params
            )
        self.reason_holdout_accuracy = None
        if len(set(ry)) > 1:
            self.reason_holdout_accuracy = _holdout_accuracy(rx, ry, self.reason_params)
        self.holdout_n = len(self.examples)

        self._save_models()

    def _save_models(self) -> None:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        if self.validity_model is not None:
            joblib.dump(self.validity_model, MODEL_DIR / "validity_model.joblib")
        if self.candidate_model is not None:
            joblib.dump(self.candidate_model, MODEL_DIR / "candidate_model.joblib")
        if self.reason_model is not None:
            joblib.dump(self.reason_model, MODEL_DIR / "reason_model.joblib")
        meta = {
            "n_examples": len(self.examples),
            "validity_params": self.validity_params,
            "candidate_params": self.candidate_params,
            "reason_params": self.reason_params,
            "cv_accuracy": self.cv_accuracy,
            "cv_accuracy_n": self.cv_accuracy_n,
            "validity_holdout_accuracy": self.validity_holdout_accuracy,
            "candidate_holdout_accuracy": self.candidate_holdout_accuracy,
            "reason_holdout_accuracy": self.reason_holdout_accuracy,
            "holdout_n": self.holdout_n,
            "saved_at": datetime.now(UTC).isoformat(),
        }
        (MODEL_DIR / "validity_meta.json").write_text(json.dumps(meta, indent=2))

    def _maybe_retrain(self) -> bool:
        """Refit (and, periodically, retune) once enough labels exist.

        First fit happens as soon as MIN_LABELS_FOR_MODEL is reached with both
        validity classes present; after that it only refits every
        RETRAIN_EVERY labels, and only re-runs hyperparameter search every
        TUNE_EVERY labels (a full search is much more expensive than a plain
        refit). Returns whether a (re)fit happened, so the caller knows the
        pool ranking is now stale.
        """
        n = len(self.examples)
        if n < MIN_LABELS_FOR_MODEL:
            return False
        vy = [e.is_valid for e in self.examples]
        if len(set(vy)) < MIN_SAMPLES_PER_CLASS:
            return False
        already_trained = self.validity_model is not None
        due = not already_trained or n % RETRAIN_EVERY == 0
        if not due:
            return False
        tune = not already_trained or n % TUNE_EVERY == 0
        self._train_and_tune(tune=tune)
        return True

    def _ensure_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scratch.trip_match_labels (
                id bigserial PRIMARY KEY,
                source text NOT NULL CHECK (source IN ('device', 'vehicle')),
                entity_id text NOT NULL,
                trip_opened_at timestamptz NOT NULL,
                trip_closed_at timestamptz NOT NULL,
                line_number text,
                resolved_feed_version_date date,
                decision text NOT NULL
                    CHECK (
                        decision IN
                        ('MATCH', 'INVALID_TRIP', 'NOT_THIS_ROUTE', 'UNSURE')
                    ),
                chosen_shape_id text,
                labeled_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (source, entity_id, trip_opened_at)
            )
            """
        )
        # additive migration for installs created before UNSURE existed - a
        # CHECK constraint can't be altered in place, only dropped and readded
        self.conn.execute(
            """
            ALTER TABLE scratch.trip_match_labels
                DROP CONSTRAINT IF EXISTS trip_match_labels_decision_check
            """
        )
        self.conn.execute(
            """
            ALTER TABLE scratch.trip_match_labels
                ADD CONSTRAINT trip_match_labels_decision_check
                CHECK (
                    decision IN ('MATCH', 'INVALID_TRIP', 'NOT_THIS_ROUTE', 'UNSURE')
                )
            """
        )

    def _count_labels(self) -> int:
        row = self.conn.execute(
            "SELECT count(*) FROM scratch.trip_match_labels"
        ).fetchone()
        return row[0] if row else 0

    def _fetch_boundary_window_keys(
        self, source: str, limit: int
    ) -> list[tuple[str, datetime, datetime]]:
        """Cold-start-only sampling, before any model exists to be uncertain with.

        Falls back to the manual "probably ambiguous" distance band as a
        sane prior. Once a validity model exists, `_fetch_worst_confidence`
        replaces this entirely - the model's own uncertainty should drive
        sampling, not a fixed heuristic band.
        """
        cfg = SOURCES[source]
        # table/column names below come only from the fixed SOURCES dict
        # (never user input), indexed by an internally-controlled source key;
        # every actual value in this query is a psycopg %()s placeholder.
        sql = f"""
            WITH sample AS (
                SELECT {cfg["key_col"]} AS entity_id, trip_opened_at, trip_closed_at,
                       avg_dist_to_line_m,
                       row_number() OVER (
                           PARTITION BY {cfg["key_col"]}, trip_opened_at
                           ORDER BY avg_dist_to_line_m ASC NULLS LAST
                       ) AS rn
                FROM {cfg["table"]} TABLESAMPLE SYSTEM (2)
                WHERE n_pings_in_window >= 3
            ),
            best AS (
                SELECT entity_id, trip_opened_at, trip_closed_at
                FROM sample
                WHERE rn = 1
                  AND avg_dist_to_line_m
                      BETWEEN {BOUNDARY_DIST_MIN_M} AND {BOUNDARY_DIST_MAX_M}
            )
            SELECT b.entity_id, b.trip_opened_at, b.trip_closed_at
            FROM best b
            WHERE NOT EXISTS (
                SELECT 1 FROM scratch.trip_match_labels l
                WHERE l.source = %(source)s
                  AND l.entity_id = b.entity_id::text
                  AND l.trip_opened_at = b.trip_opened_at
            )
            ORDER BY random()
            LIMIT %(limit)s
        """  # noqa: S608
        # sql is a dynamic f-string interpolating only fixed SOURCES-dict
        # identifiers (see comment above); psycopg's LiteralString check can't
        # express that, hence the ty:ignore below.
        rows = self.conn.execute(
            sql,  # ty:ignore[invalid-argument-type]
            {"source": source, "limit": limit},
        ).fetchall()
        return [(str(r[0]), r[1], r[2]) for r in rows]

    def _fetch_worst_confidence(self, source: str, take_n: int) -> list[Window]:
        """Broad, unfiltered sweep once a model exists.

        Samples a wide, random slice of the FULL candidate table (no
        distance-band prefilter, no "ambiguous zone" guess), scores every
        window found with the CURRENT models, and keeps `take_n` windows
        split two ways:
          - half by pure uncertainty (closest to a coin flip) - good for
            general calibration and boundary cases.
          - half by lowest predicted validity probability, REGARDLESS of how
            confident that prediction is. Pure uncertainty sampling doesn't
            reliably surface a rare class (INVALID_TRIP/NOT_THIS_ROUTE are a
            small minority of labels so far): a model can be confidently
            WRONG about a minority-class case it hasn't learned to recognize
            yet, and confidently-wrong reads as certain, not uncertain, so it
            never gets selected by uncertainty alone. This half deliberately
            forces exposure to whatever the model currently believes looks
            invalid, catching those confidently-wrong cases and correcting them.
        Pings/shapes (the expensive per-window fetch) are only pulled for the
        final selected windows, not the whole raw sample.
        """
        cfg = SOURCES[source]
        # table/column names below come only from the fixed SOURCES dict
        # (never user input), indexed by an internally-controlled source key;
        # every actual value in this query is a psycopg %()s placeholder.
        sql = f"""
            WITH sample AS (
                SELECT {cfg["key_col"]} AS entity_id, trip_opened_at, trip_closed_at,
                       row_number() OVER (
                           PARTITION BY {cfg["key_col"]}, trip_opened_at
                           ORDER BY avg_dist_to_line_m ASC NULLS LAST
                       ) AS rn
                FROM {cfg["table"]} TABLESAMPLE SYSTEM ({SWEEP_SAMPLE_PCT})
                WHERE n_pings_in_window >= 3
            )
            SELECT s.entity_id, s.trip_opened_at, s.trip_closed_at
            FROM sample s
            WHERE s.rn = 1
              AND NOT EXISTS (
                  SELECT 1 FROM scratch.trip_match_labels l
                  WHERE l.source = %(source)s
                    AND l.entity_id = s.entity_id::text
                    AND l.trip_opened_at = s.trip_opened_at
              )
        """  # noqa: S608
        # sql interpolates only fixed SOURCES-dict identifiers; see
        # _fetch_boundary_window_keys for the full rationale.
        rows = self.conn.execute(
            sql,  # ty:ignore[invalid-argument-type]
            {"source": source},
        ).fetchall()
        keys = [(str(r[0]), r[1], r[2]) for r in rows]
        if not keys:
            return []
        windows = list(self._fetch_candidates(source, keys).values())
        uncertainty, p_valid = self._score_windows(windows)

        zipped = zip(uncertainty, windows, strict=True)
        uncertainty_pairs = sorted(zipped, key=lambda p: p[0])
        by_uncertainty = [w for _, w in uncertainty_pairs]
        invalid_pairs = sorted(zip(p_valid, windows, strict=True), key=lambda p: p[0])
        by_predicted_invalid = [w for _, w in invalid_pairs]

        half = take_n // 2
        picked: list[Window] = []
        seen_keys: set[tuple[str, str, datetime]] = set()
        for w in by_uncertainty:
            if len(picked) >= half:
                break
            if w.key not in seen_keys:
                w.priority = "uncertain"
                picked.append(w)
                seen_keys.add(w.key)
        for w in by_predicted_invalid:
            if len(picked) >= take_n:
                break
            if w.key not in seen_keys:
                w.priority = "predicted_invalid"
                picked.append(w)
                seen_keys.add(w.key)
        return picked

    def _score_windows(self, windows: list[Window]) -> tuple[list[float], list[float]]:
        """Return (uncertainty, p_valid) per window.

        `uncertainty` is whichever of the three decisions is closest to a
        coin flip: validity, (for two-candidate windows leaning valid)
        which candidate, or (for windows leaning invalid) INVALID_TRIP vs
        NOT_THIS_ROUTE. `p_valid` is the raw validity probability on its
        own, used separately by the worst-confidence sweep to deliberately
        oversample windows that look invalid even when the model is
        confident about that (see _fetch_worst_confidence for why pure
        uncertainty isn't enough for a rare class).
        """
        if not windows or self.validity_model is None:
            return [0.0] * len(windows), [0.5] * len(windows)
        x = [
            w.feature_vector(self.iv_overlap_m, self.shape_start_end_dist_m)
            for w in windows
        ]
        p_valid = list(self.validity_model.predict_proba(x)[:, 1])
        uncertainty = [abs(p - 0.5) for p in p_valid]
        if self.candidate_model is not None:
            p_cand = self.candidate_model.predict_proba(x)[:, 1]
            for i, w in enumerate(windows):
                if len(w.candidates) == TWO_CANDIDATES:
                    uncertainty[i] = min(uncertainty[i], abs(p_cand[i] - 0.5))
        if self.reason_model is not None:
            p_reason = self.reason_model.predict_proba(x)[:, 1]
            for i, p in enumerate(p_valid):
                if p < PROBABILITY_THRESHOLD:
                    uncertainty[i] = min(uncertainty[i], abs(p_reason[i] - 0.5))
        return uncertainty, p_valid

    def _fetch_candidates(
        self, source: str, keys: list[tuple[str, datetime, datetime]]
    ) -> dict[tuple[str, datetime], Window]:
        cfg = SOURCES[source]
        entity_ids = [k[0] for k in keys]
        opened_ats = [k[1] for k in keys]
        # table/column names below come only from the fixed SOURCES dict
        # (never user input), indexed by an internally-controlled source key;
        # every actual value in this query is a psycopg %()s placeholder.
        sql = f"""
            SELECT t.{cfg["key_col"]}::text AS entity_id,
                   t.trip_opened_at, t.trip_closed_at,
                   t.resolved_feed_version_date, t.shape_id,
                   t.line_number, t.direction_id,
                   t.avg_dist_to_line_m, t.progress_corr,
                   t.start_proximity_m, t.end_proximity_m,
                   t.speed_percentile, t.implied_speed_kmh,
                   t.n_pings_in_window, t.duration_sec
            FROM {cfg["table"]} t
            JOIN (
                SELECT * FROM unnest(
                    %(entity_ids)s::text[], %(opened_ats)s::timestamptz[]
                ) AS k(entity_id, trip_opened_at)
            ) k
                ON t.{cfg["key_col"]}::text = k.entity_id
               AND t.trip_opened_at = k.trip_opened_at
        """  # noqa: S608
        params = {"entity_ids": entity_ids, "opened_ats": opened_ats}
        # sql interpolates only fixed SOURCES-dict identifiers; see
        # _fetch_boundary_window_keys for the full rationale.
        rows = self.conn.execute(sql, params).fetchall()  # ty:ignore[invalid-argument-type]
        windows: dict[tuple[str, datetime], Window] = {}
        for r in rows:
            win_key = (r[0], r[1])
            if win_key not in windows:
                windows[win_key] = Window(
                    source=source,
                    entity_id=r[0],
                    trip_opened_at=r[1],
                    trip_closed_at=r[2],
                    resolved_feed_version_date=r[3],
                )
            windows[win_key].candidates.append(
                Candidate(
                    shape_id=r[4],
                    line_number=r[5],
                    direction_id=r[6],
                    avg_dist_to_line_m=r[7],
                    progress_corr=r[8],
                    start_proximity_m=r[9],
                    end_proximity_m=r[10],
                    speed_percentile=r[11],
                    implied_speed_kmh=r[12],
                    n_pings_in_window=r[13],
                    duration_sec=float(r[14]) if r[14] is not None else None,
                )
            )
        return windows

    def _fetch_pings(self, source: str, windows: list[Window]) -> None:
        if not windows:
            return
        entity_ids = [w.entity_id for w in windows]
        opened_ats = [w.trip_opened_at for w in windows]
        closed_ats = [w.trip_closed_at for w in windows]
        # ping_key_expr/entity_cast are always one of two hardcoded literals
        # (never user input); every actual value below is a psycopg %()s
        # placeholder.
        ping_key_expr = "device_id" if source == "device" else "vehicle_id"
        entity_cast = "k.entity_id" if source == "device" else "k.entity_id::integer"
        sql = f"""
            SELECT k.entity_id, k.trip_opened_at,
                   p.metric_timestamp, p.latitude, p.longitude
            FROM unnest(
                %(entity_ids)s::text[],
                %(opened_ats)s::timestamptz[],
                %(closed_ats)s::timestamptz[]
            ) AS k(entity_id, trip_opened_at, trip_closed_at)
            JOIN LATERAL (
                SELECT metric_timestamp, latitude, longitude
                FROM silver.avl_pings
                WHERE {ping_key_expr} = {entity_cast}
                  AND metric_timestamp BETWEEN k.trip_opened_at AND k.trip_closed_at
                ORDER BY metric_timestamp
            ) p ON true
        """  # noqa: S608
        params = {
            "entity_ids": entity_ids,
            "opened_ats": opened_ats,
            "closed_ats": closed_ats,
        }
        # sql interpolates only ping_key_expr/entity_cast, both ternaries
        # between literal strings, so ty can prove this one safe on its own.
        rows = self.conn.execute(sql, params).fetchall()
        by_key: dict[tuple[str, datetime], list[tuple[datetime, float, float]]] = {}
        for r in rows:
            by_key.setdefault((r[0], r[1]), []).append((r[2], r[3], r[4]))
        for w in windows:
            w.pings = by_key.get((w.entity_id, w.trip_opened_at), [])

    def _fetch_shapes(self, windows: list[Window]) -> None:
        triples = {
            (w.resolved_feed_version_date, c.line_number, c.shape_id)
            for w in windows
            for c in w.candidates
        }
        if not triples:
            return
        fds = [t[0] for t in triples]
        lns = [t[1] for t in triples]
        sids = [t[2] for t in triples]
        sql = """
            SELECT g.feed_version_date, g.line_number, g.shape_id,
                   ST_AsGeoJSON(g.line_geom)
            FROM scratch.route_shape_geoms g
            JOIN (
                SELECT * FROM unnest(%(fds)s::date[], %(lns)s::text[], %(sids)s::text[])
                    AS k(feed_version_date, line_number, shape_id)
            ) k ON g.feed_version_date = k.feed_version_date
               AND g.line_number = k.line_number
               AND g.shape_id = k.shape_id
        """
        params = {"fds": fds, "lns": lns, "sids": sids}
        rows = self.conn.execute(sql, params).fetchall()
        geoms: dict[tuple[date, str, str], list[tuple[float, float]]] = {}
        for r in rows:
            coords = json.loads(r[3])["coordinates"]
            geoms[(r[0], r[1], r[2])] = [(lat, lon) for lon, lat in coords]
        for w in windows:
            for c in w.candidates:
                key = (w.resolved_feed_version_date, c.line_number, c.shape_id)
                pts = geoms.get(key)
                if pts:
                    w.shapes[c.shape_id] = pts

    def _refill(self) -> None:
        for _ in range(len(self._source_cycle)):
            source = self._source_cycle[0]
            self._source_cycle.append(self._source_cycle.pop(0))
            if self.validity_model is not None:
                # a real model exists - sweep the full table and keep the
                # actual worst-confidence windows, no manual filters
                windows = self._fetch_worst_confidence(source, BATCH_FETCH)
            else:
                # cold start - no model to be uncertain with yet, use the
                # heuristic band as a sane prior
                keys = self._fetch_boundary_window_keys(source, BATCH_FETCH)
                windows = []
                if keys:
                    windows = list(self._fetch_candidates(source, keys).values())
            self._fetch_pings(source, windows)
            self._fetch_shapes(windows)
            self.pool.extend(windows)
        if self.validity_model is not None:
            self._rank_pool()
        else:
            random.shuffle(self.pool)

    def _rank_pool(self) -> None:
        """Re-sort the pool for serve order.

        Alternates between the two priority groups instead of one flat
        uncertainty sort. A flat sort by uncertainty alone would bury every
        "predicted_invalid" window (see _fetch_worst_confidence) at the back
        of a ~500-item queue, since by definition those are LESS ambiguous
        than boundary cases even though seeing them is exactly the point -
        they're the confidently-wrong
        minority-class cases uncertainty sampling can't find on its own. So
        each group is sorted by its own relevant score, then interleaved,
        which guarantees you actually encounter both kinds regularly rather
        than one monopolizing everything you see for hundreds of labels.
        """
        if not self.pool or self.validity_model is None:
            return
        uncertainty, p_valid = self._score_windows(self.pool)
        uncertain_group = sorted(
            (
                (uncertainty[i], w)
                for i, w in enumerate(self.pool)
                if w.priority == "uncertain"
            ),
            key=lambda p: p[0],
        )
        invalid_group = sorted(
            (
                (p_valid[i], w)
                for i, w in enumerate(self.pool)
                if w.priority == "predicted_invalid"
            ),
            key=lambda p: p[0],
        )
        uncertain_list = [w for _, w in uncertain_group]
        invalid_list = [w for _, w in invalid_group]
        interleaved: list[Window] = []
        for a, b in zip(uncertain_list, invalid_list, strict=False):
            interleaved.extend((a, b))
        interleaved.extend(uncertain_list[len(invalid_list) :])
        interleaved.extend(invalid_list[len(uncertain_list) :])
        self.pool = interleaved

    def next_window(self) -> Window:
        """Pop the most useful window to label next, refilling the pool if low."""
        with self.lock:
            if len(self.pool) < POOL_REFILL_AT:
                self._refill()
            if not self.pool:
                detail = "pool exhausted, nothing left to label"
                raise HTTPException(status_code=404, detail=detail)
            self.current = self.pool.pop(0)
            return self.current

    def submit_label(self, payload: LabelIn) -> None:
        """Persist a decision, fold it into the training set, and retrain if ready."""
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO scratch.trip_match_labels
                    (source, entity_id, trip_opened_at, trip_closed_at, line_number,
                     resolved_feed_version_date, decision, chosen_shape_id)
                VALUES (%(source)s, %(entity_id)s,
                        %(trip_opened_at)s, %(trip_closed_at)s,
                        %(line_number)s, %(resolved_feed_version_date)s,
                        %(decision)s, %(chosen_shape_id)s)
                ON CONFLICT (source, entity_id, trip_opened_at) DO UPDATE
                    SET decision = EXCLUDED.decision,
                        chosen_shape_id = EXCLUDED.chosen_shape_id,
                        labeled_at = now()
                """,
                payload.model_dump(),
            )
            self.n_labels += 1

            if self.current is not None and self.current.key == (
                payload.source,
                payload.entity_id,
                payload.trip_opened_at,
            ):
                if payload.decision != "UNSURE":
                    self.examples.append(
                        _build_example(
                            self.current,
                            payload.decision,
                            payload.chosen_shape_id,
                            self.iv_overlap_m,
                            self.shape_start_end_dist_m,
                        )
                    )
                self.current = None

            if self._maybe_retrain():
                self._rank_pool()

    def stats(self) -> dict[str, Any]:
        """Return current progress counters for the UI header."""
        return {
            "n_labels": self.n_labels,
            "pool_size": len(self.pool),
            "validity_trained": self.validity_model is not None,
            "candidate_trained": self.candidate_model is not None,
            "reason_trained": self.reason_model is not None,
            "n_training_examples": len(self.examples),
            "cv_accuracy": self.cv_accuracy,
            "cv_accuracy_n": self.cv_accuracy_n,
            "validity_holdout_accuracy": self.validity_holdout_accuracy,
            "candidate_holdout_accuracy": self.candidate_holdout_accuracy,
            "reason_holdout_accuracy": self.reason_holdout_accuracy,
            "holdout_n": self.holdout_n,
        }


store = LabelStore(DSN)
app = FastAPI()


def _window_to_json(w: Window) -> dict[str, Any]:
    # best_candidate() is internal plumbing only now: it's the anchor the
    # feature vector and the candidate model's training target are defined
    # around. It is NOT exposed to the UI as "naive best" - the only thing
    # shown is the model's actual prediction + confidence, when one exists.
    best = w.best_candidate()
    x = w.feature_vector(store.iv_overlap_m, store.shape_start_end_dist_m)

    validity_pred: dict[str, Any] | None = None
    if store.validity_model is not None:
        p_valid = float(store.validity_model.predict_proba([x])[0][1])
        validity_pred = {
            "predicted_valid": p_valid >= PROBABILITY_THRESHOLD,
            "valid_probability": round(p_valid, 4),
        }

    candidate_pred: dict[str, Any] | None = None
    model_pick_shape_id: str | None = None  # set only when candidate_model exists
    if store.candidate_model is not None and len(w.candidates) == TWO_CANDIDATES:
        p_naive_best = float(store.candidate_model.predict_proba([x])[0][1])
        other = next(c for c in w.candidates if c is not best)
        if p_naive_best >= PROBABILITY_THRESHOLD:
            model_pick_shape_id = best.shape_id
            predicted_probability = p_naive_best
        else:
            model_pick_shape_id = other.shape_id
            predicted_probability = 1 - p_naive_best
        candidate_pred = {
            "predicted_shape_id": model_pick_shape_id,
            "predicted_probability": round(predicted_probability, 4),
        }

    reason_pred: dict[str, Any] | None = None
    if (
        store.reason_model is not None
        and validity_pred is not None
        and not validity_pred["predicted_valid"]
    ):
        p_not_this_route = float(store.reason_model.predict_proba([x])[0][1])
        predicted_reason = (
            "NOT_THIS_ROUTE"
            if p_not_this_route >= PROBABILITY_THRESHOLD
            else "INVALID_TRIP"
        )
        reason_pred = {
            "predicted_reason": predicted_reason,
            "predicted_probability": round(
                p_not_this_route
                if p_not_this_route >= PROBABILITY_THRESHOLD
                else 1 - p_not_this_route,
                4,
            ),
        }

    return {
        "source": w.source,
        "entity_id": w.entity_id,
        "trip_opened_at": w.trip_opened_at.isoformat(),
        "trip_closed_at": w.trip_closed_at.isoformat(),
        "line_number": best.line_number,
        "resolved_feed_version_date": w.resolved_feed_version_date.isoformat(),
        "priority": w.priority,
        "model_prediction": {
            "validity": validity_pred,
            "candidate": candidate_pred,
            "reason": reason_pred,
        },
        "candidates": [
            {
                "shape_id": c.shape_id,
                "direction_id": c.direction_id,
                "is_model_pick": (
                    model_pick_shape_id is not None
                    and c.shape_id == model_pick_shape_id
                ),
                "avg_dist_to_line_m": _clean(c.avg_dist_to_line_m),
                "progress_corr": _clean(c.progress_corr),
                "start_proximity_m": _clean(c.start_proximity_m),
                "end_proximity_m": _clean(c.end_proximity_m),
                "speed_percentile": _clean(c.speed_percentile),
                "implied_speed_kmh": _clean(c.implied_speed_kmh),
                "n_pings_in_window": c.n_pings_in_window,
                "duration_sec": c.duration_sec,
                "path": w.shapes.get(c.shape_id, []),
            }
            for c in w.candidates
        ],
        "pings": [
            {"t": t.isoformat(), "lat": lat, "lon": lon} for t, lat, lon in w.pings
        ],
        "stats": store.stats(),
    }


@app.get("/")
def index() -> FileResponse:
    """Serve the labeling UI."""
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/next")
def api_next() -> JSONResponse:
    """Return the next window to label, ranked by model uncertainty when available."""
    w = store.next_window()
    return JSONResponse(_window_to_json(w))


@app.post("/api/label")
def api_label(payload: LabelIn) -> dict[str, Any]:
    """Record a labeling decision for the current window."""
    store.submit_label(payload)
    return {"ok": True, "stats": store.stats()}


@app.get("/api/metrics")
def api_metrics() -> dict[str, Any]:
    """Compute and persist full precision/recall/F1 metrics for both models.

    Uses the current in-memory label set (store.examples), so it's always
    consistent with whatever the running app has actually loaded. Written to
    model_store/metrics.json on every call, alongside the deployed models.
    """
    metrics = _classification_metrics(store.examples)
    metrics["computed_at"] = datetime.now(UTC).isoformat()
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    (MODEL_DIR / "metrics.json").write_text(json.dumps(metrics, indent=2))
    return metrics


if __name__ == "__main__":
    # tailnet-only dev box, matches Adminer's own exposure pattern
    uvicorn.run(app, host="0.0.0.0", port=8010)  # noqa: S104
