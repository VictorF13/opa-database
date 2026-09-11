"""LightGBM training for the Bus Matching model.

Per the plan's Section 5: fixed hyperparameters for the entire active
learning loop (tuning every round costs minutes and buys nothing while
the labeled set is still growing), and no feature selection (the
constraint-generated negatives already give plenty of training rows,
and the features are largely non-redundant by construction). One
proper hyperparameter tuning pass happens only after labeling
converges -- not implemented yet, since labeling hasn't produced enough
rows for that to matter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import lightgbm as lgb
import numpy as np

if TYPE_CHECKING:
    import pandas as pd

FIXED_HYPERPARAMETERS: dict[str, object] = {
    "num_leaves": 31,
    "learning_rate": 0.05,
    "n_estimators": 300,
    "min_child_samples": 20,
}


def train_model(features: pd.DataFrame, y: np.ndarray) -> lgb.LGBMClassifier:
    """Fit a LightGBM binary classifier with the loop's fixed hyperparameters.

    Args:
        features: Training pool feature columns (every Tier 1 feature).
        y: Training pool labels.

    Returns:
        The fitted model.

    """
    model = lgb.LGBMClassifier(
        objective="binary", verbosity=-1, **FIXED_HYPERPARAMETERS
    )
    model.fit(features, y)
    return model


def predict_positive_proba(
    model: lgb.LGBMClassifier, features: pd.DataFrame
) -> np.ndarray:
    """Predict P(this candidate is the real match) as a plain ndarray.

    Args:
        model: A fitted LightGBM classifier.
        features: Rows to score, columns matching what `model` was fit on.

    Returns:
        The model's raw (uncalibrated) predicted probability of the
        positive class.

    """
    return np.asarray(model.predict_proba(features))[:, 1]
