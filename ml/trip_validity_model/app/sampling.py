"""Active-learning candidate draw logic: phase detection + uncertain/random mix."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

import db
import numpy as np

if TYPE_CHECKING:
    import pandas as pd
    import psycopg

SEED_SIZE = 50
CALIBRATION_SIZE = 50
TEST_SIZE = 50
UNCERTAIN_DRAW_PROBABILITY = 0.75


@dataclass
class Candidate:
    """One trip drawn for labeling."""

    trip_id: int
    label_set: db.LabelSet
    selection_source: db.SelectionSource


def current_phase(counts: dict[db.LabelSet, int]) -> str:
    """Determine which of the four labeling phases is currently active.

    Args:
        counts: Output of `db.label_set_counts`.

    Returns:
        One of "calibration", "test", "seed", "active".

    """
    if counts["calibration"] < CALIBRATION_SIZE:
        return "calibration"
    if counts["test"] < TEST_SIZE:
        return "test"
    if counts["train"] < SEED_SIZE:
        return "seed"
    return "active"


def draw_candidate(
    conn: psycopg.Connection, uncertain_queue: list[int]
) -> Candidate | None:
    """Draw the next trip to show the labeler.

    During the "calibration"/"test"/"seed" phases every draw is
    uniformly random. During the "active" phase, most draws pop the
    front of `uncertain_queue` (the current model's most-uncertain
    remaining predictions, refreshed at each retrain); the rest are
    random, so labeling stays a blind mix of both selection sources. A
    skipped trip is never recorded anywhere, so it may resurface in a
    later draw exactly like any other unlabeled trip.

    Args:
        conn: An open connection.
        uncertain_queue: Mutated in place — a trip_id popped from here
            is removed.

    Returns:
        The next `Candidate`, or `None` if nothing is left to label.

    """
    counts = db.label_set_counts(conn)
    phase = current_phase(counts)
    label_set: db.LabelSet
    if phase == "calibration":
        label_set = "calibration"
    elif phase == "test":
        label_set = "test"
    else:
        label_set = "train"

    trip_id: int | None = None
    source: db.SelectionSource = "random"

    if phase == "active" and random.random() < UNCERTAIN_DRAW_PROBABILITY:  # noqa: S311
        source = "uncertain"
        while uncertain_queue:
            candidate_id = uncertain_queue.pop(0)
            if db.is_unlabeled(conn, candidate_id):
                trip_id = candidate_id
                break

    if trip_id is None:
        trip_id = db.fetch_random_unlabeled_trip_id(conn)
        source = "random"

    if trip_id is None:
        return None
    return Candidate(trip_id=trip_id, label_set=label_set, selection_source=source)


def build_uncertain_queue(
    unlabeled_pool: pd.DataFrame,
    calibrated_probabilities: np.ndarray,
    *,
    top_n: int = 200,
) -> list[int]:
    """Rank remaining candidates by distance from 0.5 and cache the most uncertain.

    Args:
        unlabeled_pool: Must include a `trip_id` column, in the same row
            order as `calibrated_probabilities`.
        calibrated_probabilities: This model's calibrated P(valid) for
            each row of `unlabeled_pool`.
        top_n: How many of the most-uncertain trip_ids to cache.

    Returns:
        `trip_id`s ordered most to least uncertain.

    """
    uncertainty = -np.abs(calibrated_probabilities - 0.5)
    order = np.argsort(uncertainty)[::-1][:top_n]
    return unlabeled_pool["trip_id"].to_numpy()[order].tolist()


def landmark_crossed(n_train_labels: int) -> str | None:
    """Check whether the training pool just crossed a retrain/retune landmark.

    Args:
        n_train_labels: Training pool size *after* the label was added.

    Returns:
        "milestone" every 50 labels (this also covers the very first
        model, trained once the 50-row seed pool is complete), "cycle"
        every 15 labels otherwise, `None` if neither (or the seed pool
        isn't complete yet).

    """
    if n_train_labels < SEED_SIZE:
        return None
    if n_train_labels % 50 == 0:
        return "milestone"
    if n_train_labels % 15 == 0:
        return "cycle"
    return None
