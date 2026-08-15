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
# Active-phase draws split so training and calibration+test grow at the
# same overall rate, while calibration/test only ever receive genuinely
# random rows (never uncertainty-picked ones, which would bias
# evaluation): 1/3 uncertain -> train, and of the other 2/3 (random),
# 1/4 -> train (diversity) / 3/4 -> calibration or test, whichever is
# smaller. Overall that's exactly 1/3 + 2/3*1/4 = 1/2 -> train and
# 2/3*3/4 = 1/2 -> calibration+test.
UNCERTAIN_DRAW_PROBABILITY = 1 / 3
RANDOM_DRAW_TRAIN_FRACTION = 1 / 4

# Hard caps for the 500-label budget: 250 train + 125 calibration + 125
# test. Once a set hits its cap it's excluded from selection entirely -
# draws that would've gone there get redirected to whichever open set
# is furthest below its own target, so the final split lands exactly on
# these numbers instead of just converging toward them.
TRAIN_CAP = 250
CALIBRATION_CAP = 125
TEST_CAP = 125
_CAPS: dict[db.LabelSet, int] = {
    "train": TRAIN_CAP,
    "calibration": CALIBRATION_CAP,
    "test": TEST_CAP,
}
_ALL_LABEL_SETS: tuple[db.LabelSet, ...] = ("train", "calibration", "test")


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


def _random_candidate(
    conn: psycopg.Connection, label_set: db.LabelSet
) -> Candidate | None:
    trip_id = db.fetch_random_unlabeled_trip_id(conn)
    if trip_id is None:
        return None
    return Candidate(trip_id=trip_id, label_set=label_set, selection_source="random")


def draw_candidate(
    conn: psycopg.Connection, uncertain_queue: list[int]
) -> Candidate | None:
    """Draw the next trip to show the labeler.

    During the "calibration"/"test"/"seed" phases every draw is
    uniformly random and feeds that phase's own set. During "active"
    (see module docstring constants for the exact split), calibration
    and test only ever grow from genuinely random draws - appended to
    whichever currently has fewer rows, never reassigning a row already
    labeled - so evaluation stays unbiased by the model's own picks.
    Once a set hits its cap (`TRAIN_CAP`/`CALIBRATION_CAP`/`TEST_CAP`)
    it stops receiving draws; anything that would've gone there is
    redirected to whichever open set is furthest below its own target,
    so the budget finishes at exactly 250/125/125 rather than merely
    converging toward it. `None` once all three are full. A skipped
    trip is never recorded anywhere, so it may resurface in a later
    draw exactly like any other unlabeled trip.

    Args:
        conn: An open connection.
        uncertain_queue: Mutated in place — a trip_id popped from here
            is removed.

    Returns:
        The next `Candidate`, or `None` if nothing is left to label.

    """
    counts = db.label_set_counts(conn)
    phase = current_phase(counts)

    if phase == "calibration":
        return _random_candidate(conn, "calibration")
    if phase == "test":
        return _random_candidate(conn, "test")
    if phase == "seed":
        return _random_candidate(conn, "train")

    # Remaining case: phase is "active".
    open_sets = [s for s in _ALL_LABEL_SETS if counts[s] < _CAPS[s]]
    if not open_sets:
        return None

    if "train" in open_sets and random.random() < UNCERTAIN_DRAW_PROBABILITY:  # noqa: S311
        while uncertain_queue:
            candidate_id = uncertain_queue.pop(0)
            if db.is_unlabeled(conn, candidate_id):
                return Candidate(
                    trip_id=candidate_id,
                    label_set="train",
                    selection_source="uncertain",
                )
        # Queue exhausted between retrains: fall through to a random draw.

    if random.random() < RANDOM_DRAW_TRAIN_FRACTION:  # noqa: S311
        natural_target: db.LabelSet = "train"
    else:
        natural_target = (
            "calibration" if counts["calibration"] <= counts["test"] else "test"
        )

    target = (
        natural_target if natural_target in open_sets else _neediest(open_sets, counts)
    )
    return _random_candidate(conn, target)


def _neediest(
    open_sets: list[db.LabelSet], counts: dict[db.LabelSet, int]
) -> db.LabelSet:
    """Pick whichever open set is proportionally furthest below its own cap."""
    return min(open_sets, key=lambda s: counts[s] / _CAPS[s])


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
