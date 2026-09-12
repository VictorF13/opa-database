"""Evaluation metrics for the Trip Validity model: AUC, Brier, log-loss, ECE."""

from __future__ import annotations

import numpy as np
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score


def expected_calibration_error(
    y_true: np.ndarray, y_prob: np.ndarray, *, n_bins: int = 10
) -> float:
    """Compute the expected calibration error via equal-width probability bins.

    Args:
        y_true: True binary labels.
        y_prob: Predicted probabilities of the positive class.
        n_bins: Number of equal-width bins over `[0, 1]`.

    Returns:
        The sample-weighted mean absolute gap between each bin's average
        predicted probability and its actual positive rate.

    """
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    bin_indices = np.clip(np.digitize(y_prob, bin_edges[1:-1]), 0, n_bins - 1)
    total = len(y_true)
    ece = 0.0
    for b in range(n_bins):
        mask = bin_indices == b
        if not mask.any():
            continue
        bin_confidence = y_prob[mask].mean()
        bin_accuracy = y_true[mask].mean()
        ece += (mask.sum() / total) * abs(bin_confidence - bin_accuracy)
    return float(ece)


def evaluate(y_true: np.ndarray, y_prob: np.ndarray) -> dict[str, float]:
    """Compute every reported test-set metric at once.

    Args:
        y_true: True binary labels.
        y_prob: Calibrated predicted probabilities of the positive class.

    Returns:
        Dict with keys "auc", "brier", "log_loss", "ece". "auc" is
        `NaN` if `y_true` has only one class present (undefined
        otherwise).

    """
    auc = (
        float(roc_auc_score(y_true, y_prob))
        if len(set(y_true.tolist())) > 1
        else float("nan")
    )
    return {
        "auc": auc,
        "brier": float(brier_score_loss(y_true, y_prob)),
        "log_loss": float(log_loss(y_true, y_prob, labels=[False, True])),
        "ece": expected_calibration_error(y_true, y_prob),
    }


def confident_prediction_count(
    y_prob: np.ndarray, *, threshold: float = 0.9
) -> tuple[int, int]:
    """Count predictions confidently on either side of 0.5.

    Args:
        y_prob: Calibrated predicted probabilities of the positive class.
        threshold: Confidence cutoff, e.g. 0.9 = "at least 90% sure either way".

    Returns:
        `(confident_count, total_count)`.

    """
    confident = (y_prob >= threshold) | (y_prob <= 1 - threshold)
    return int(confident.sum()), len(y_prob)
