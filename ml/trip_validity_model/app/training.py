"""LightGBM training: per-cycle retrain, and the periodic Optuna milestone.

The milestone jointly tunes LightGBM hyperparameters and how many of the
training pool's features to keep, ranked by CV gain importance. A literal
exhaustive search over the ~66 candidate features is combinatorially
impossible (2**66 subsets); tuning a single "keep the top K by
importance" knob inside the same Optuna study is the tractable
equivalent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import lightgbm as lgb
import numpy as np
import optuna
import pandas as pd
from sklearn.metrics import brier_score_loss
from sklearn.model_selection import StratifiedKFold, train_test_split

optuna.logging.set_verbosity(optuna.logging.WARNING)

N_CV_FOLDS = 5
MAX_ESTIMATORS = 500
EARLY_STOPPING_ROUNDS = 30
EARLY_STOPPING_HOLDOUT_FRACTION = 0.2
MIN_CLASS_MEMBERS_TO_STRATIFY = 2

_INT_SEARCH_SPACE: dict[str, tuple[int, int]] = {
    "num_leaves": (7, 63),
    "min_child_samples": (5, 40),
}
_FLOAT_SEARCH_SPACE: dict[str, tuple[float, float]] = {
    "learning_rate": (0.01, 0.3),
    "feature_fraction": (0.5, 1.0),
    "bagging_fraction": (0.5, 1.0),
    "lambda_l1": (0.0, 5.0),
    "lambda_l2": (0.0, 5.0),
}


@dataclass
class ModelConfig:
    """The LightGBM configuration active between milestones."""

    selected_features: list[str]
    hyperparameters: dict[str, Any]


@dataclass
class MilestoneResult:
    """Output of a full Optuna hyperparameter + feature-selection retune."""

    config: ModelConfig
    cv_brier_score: float
    feature_importance_ranking: list[str]


def predict_positive_proba(
    model: lgb.LGBMClassifier, features: pd.DataFrame
) -> np.ndarray:
    """Predict P(positive class) as a plain ndarray.

    LightGBM's sklearn-wrapper type stubs claim `predict_proba` can
    return a sparse matrix, which it never does for a dense pandas
    input; `np.asarray` here is purely to satisfy the type checker.

    Args:
        model: A fitted LightGBM classifier.
        features: Rows to score, columns matching what `model` was fit on.

    Returns:
        The model's raw (uncalibrated) predicted probability of the
        positive class.

    """
    return np.asarray(model.predict_proba(features))[:, 1]


def _stratify_target(y: np.ndarray) -> np.ndarray | None:
    """Return `y` for `stratify=` if every class has enough members, else `None`."""
    _, counts = np.unique(y, return_counts=True)
    return y if counts.min() >= MIN_CLASS_MEMBERS_TO_STRATIFY else None


def _fit_with_early_stopping(
    features: pd.DataFrame, y: np.ndarray, hyperparameters: dict[str, Any]
) -> lgb.LGBMClassifier:
    """Fit one model, holding out a slice purely to pick the tree count."""
    fit_features, stop_features, fit_y, stop_y = train_test_split(
        features,
        y,
        test_size=EARLY_STOPPING_HOLDOUT_FRACTION,
        random_state=42,
        stratify=_stratify_target(y),
    )
    model = lgb.LGBMClassifier(
        objective="binary", n_estimators=MAX_ESTIMATORS, verbosity=-1, **hyperparameters
    )
    model.fit(
        fit_features,
        fit_y,
        eval_X=stop_features,
        eval_y=stop_y,
        callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
    )
    return model


def _cross_validate(
    features: pd.DataFrame, y: np.ndarray, hyperparameters: dict[str, Any]
) -> tuple[float, int]:
    """Run stratified CV, each fold with its own early-stopping split.

    Returns:
        `(out_of_fold_brier_score, mean_best_iteration)`.

    """
    folds = StratifiedKFold(n_splits=N_CV_FOLDS, shuffle=True, random_state=42)
    oof_predictions = np.empty(len(y))
    best_iterations = []
    for train_idx, valid_idx in folds.split(features, y):
        model = _fit_with_early_stopping(
            features.iloc[train_idx], y[train_idx], hyperparameters
        )
        oof_predictions[valid_idx] = predict_positive_proba(
            model, features.iloc[valid_idx]
        )
        best_iterations.append(model.best_iteration_ or MAX_ESTIMATORS)
    return float(brier_score_loss(y, oof_predictions)), int(np.mean(best_iterations))


def _rank_features_by_importance(features: pd.DataFrame, y: np.ndarray) -> list[str]:
    """Rank every candidate feature by mean CV gain importance.

    Args:
        features: Full feature frame (every candidate column).
        y: True labels.

    Returns:
        Feature names ordered most to least important.

    """
    folds = StratifiedKFold(n_splits=N_CV_FOLDS, shuffle=True, random_state=42)
    importances = pd.Series(0.0, index=features.columns)
    for train_idx, _ in folds.split(features, y):
        model = _fit_with_early_stopping(features.iloc[train_idx], y[train_idx], {})
        importances += pd.Series(
            model.booster_.feature_importance(importance_type="gain"),
            index=features.columns,
        )
    return importances.sort_values(ascending=False).index.tolist()


def run_milestone(
    features: pd.DataFrame,
    y: np.ndarray,
    *,
    n_trials: int = 40,
    timeout_seconds: float = 90,
) -> MilestoneResult:
    """Jointly tune LightGBM hyperparameters and a top-K feature subset.

    Runs one baseline CV fit over every candidate feature to rank them by
    gain importance, then an Optuna study (minimizing out-of-fold Brier
    score) over both LightGBM hyperparameters and how many of the
    top-ranked features to keep.

    Args:
        features: Full feature frame for the current training pool.
        y: Full training pool labels.
        n_trials: Optuna trial budget.
        timeout_seconds: Wall-clock budget, so this stays interactive.

    Returns:
        The winning `ModelConfig` plus its CV score and the full
        importance ranking (for the UI's "top features" display).

    """
    ranking = _rank_features_by_importance(features, y)

    def objective(trial: optuna.Trial) -> float:
        top_k = trial.suggest_int("top_k", min(5, len(ranking)), len(ranking))
        hyperparameters = {
            "num_leaves": trial.suggest_int(
                "num_leaves", *_INT_SEARCH_SPACE["num_leaves"]
            ),
            "min_child_samples": trial.suggest_int(
                "min_child_samples", *_INT_SEARCH_SPACE["min_child_samples"]
            ),
            "learning_rate": trial.suggest_float(
                "learning_rate", *_FLOAT_SEARCH_SPACE["learning_rate"], log=True
            ),
            "feature_fraction": trial.suggest_float(
                "feature_fraction", *_FLOAT_SEARCH_SPACE["feature_fraction"]
            ),
            "bagging_fraction": trial.suggest_float(
                "bagging_fraction", *_FLOAT_SEARCH_SPACE["bagging_fraction"]
            ),
            "lambda_l1": trial.suggest_float(
                "lambda_l1", *_FLOAT_SEARCH_SPACE["lambda_l1"]
            ),
            "lambda_l2": trial.suggest_float(
                "lambda_l2", *_FLOAT_SEARCH_SPACE["lambda_l2"]
            ),
        }
        oof_brier, n_estimators = _cross_validate(
            features[ranking[:top_k]], y, hyperparameters
        )
        trial.set_user_attr("n_estimators", n_estimators)
        return oof_brier

    study = optuna.create_study(direction="minimize")
    study.optimize(objective, n_trials=n_trials, timeout=timeout_seconds)

    best = study.best_trial
    top_k = best.params["top_k"]
    hyperparameters = {k: v for k, v in best.params.items() if k != "top_k"}
    hyperparameters["n_estimators"] = best.user_attrs["n_estimators"]

    return MilestoneResult(
        config=ModelConfig(
            selected_features=ranking[:top_k], hyperparameters=hyperparameters
        ),
        cv_brier_score=study.best_value,
        feature_importance_ranking=ranking,
    )


def train_final_model(
    features: pd.DataFrame, y: np.ndarray, config: ModelConfig
) -> lgb.LGBMClassifier:
    """Fit the model actually served for predictions, on the full training pool.

    Uses `config.hyperparameters["n_estimators"]` as a fixed tree count
    (chosen by the last milestone's CV + early stopping) rather than
    re-running early stopping, matching the "retrain from scratch, not
    incremental" rule for regular cycles.

    Args:
        features: Full training pool feature frame.
        y: Full training pool labels.
        config: The currently active feature subset + hyperparameters.

    Returns:
        The fitted model.

    """
    hyperparameters = dict(config.hyperparameters)
    n_estimators = hyperparameters.pop("n_estimators", MAX_ESTIMATORS)
    model = lgb.LGBMClassifier(
        objective="binary", n_estimators=n_estimators, verbosity=-1, **hyperparameters
    )
    model.fit(features[config.selected_features], y)
    return model
