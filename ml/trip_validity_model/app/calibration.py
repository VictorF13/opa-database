"""Platt scaling: a 1-D logistic regression recalibrating raw model probabilities."""

from __future__ import annotations

import numpy as np
from sklearn.linear_model import LogisticRegression

_EPS = 1e-6


def _logit(probabilities: np.ndarray) -> np.ndarray:
    clipped = np.clip(probabilities, _EPS, 1 - _EPS)
    return np.log(clipped / (1 - clipped))


class PlattCalibrator:
    """Recalibrates raw model probabilities via logistic regression on their logit."""

    def __init__(self) -> None:
        """Initialize the underlying 1-D logistic regression, unfit."""
        self._model = LogisticRegression()

    def fit(self, raw_probabilities: np.ndarray, y_true: np.ndarray) -> PlattCalibrator:
        """Fit the calibrator on a frozen calibration set.

        Args:
            raw_probabilities: The base model's raw predicted probabilities.
            y_true: The true labels for the same rows.

        Returns:
            self, for chaining.

        """
        self._model.fit(_logit(raw_probabilities).reshape(-1, 1), y_true)
        return self

    def predict(self, raw_probabilities: np.ndarray) -> np.ndarray:
        """Recalibrate raw probabilities.

        Args:
            raw_probabilities: The base model's raw predicted probabilities.

        Returns:
            Calibrated probabilities of the positive class.

        """
        logits = _logit(raw_probabilities).reshape(-1, 1)
        return self._model.predict_proba(logits)[:, 1]

    def to_params(self) -> dict[str, float]:
        """Serialize the fitted coefficient/intercept for storage.

        Returns:
            A dict with keys "coefficient" and "intercept".

        """
        return {
            "coefficient": float(self._model.coef_[0, 0]),
            "intercept": float(self._model.intercept_[0]),
        }

    def to_sklearn_model(self) -> LogisticRegression:
        """Return the underlying fitted sklearn model.

        For artifact storage: pickling this class directly ties the
        artifact to *this exact* `PlattCalibrator` class object, which
        breaks (`PicklingError`) if the `calibration` module gets
        hot-reloaded (e.g. by Streamlit's file watcher during dev)
        between when an instance was created and when it's pickled -
        the reloaded module's class is a distinct object with the same
        name. Storing/restoring the plain sklearn model instead
        sidesteps that entirely.

        Returns:
            The fitted `LogisticRegression`.

        """
        return self._model

    @classmethod
    def from_sklearn_model(cls, model: LogisticRegression) -> PlattCalibrator:
        """Reconstruct a calibrator from a model saved via `to_sklearn_model`.

        Args:
            model: A previously fitted `LogisticRegression`.

        Returns:
            A `PlattCalibrator` wrapping it.

        """
        calibrator = cls()
        calibrator._model = model
        return calibrator
