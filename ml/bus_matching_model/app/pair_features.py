"""Month-level `(bus_id, device_id)` features -- the pair model's input.

The day-level model answers "did this device drive this bus *on this
date*". That was the right unit while the pipeline was per-date, but a
device essentially never changes bus mid-month, so the question that
actually needs answering is "is this device this bus's device", full
stop. This module aggregates every day's evidence into one row per
candidate pair, which is both the real prediction unit and a much
stronger signal: a single ambiguous day is noise, but twenty days of
consistent mild evidence is close to conclusive.

**Deliberately excludes Section 9/10 output** (`ml.bus_matching_global_assignment`,
`ml.bus_matching_intervals`). Those are downstream of the day model,
and the pair model is meant to *replace* the hand-weighted belief layer
that produced them -- feeding their output back in as features would
make the final confidence partly a function of the very heuristics it
exists to retire. Everything here is derived from raw day features,
the day model's own scores, the dictionaries, and candidate
competition.

Every signal that used to be a hand-set constant in `belief.py`
(dictionary trust, temporal continuity, cross-bus suppression) appears
here as a plain feature instead, so the pair model learns its weight
from labels rather than inheriting a number someone guessed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    import psycopg

DAY_SCORE_HIT_THRESHOLD = 0.5

# Day features worth carrying up to the pair level. Not all 31 -- the
# per-day counts (n_trips_sampled etc.) are summarized by the coverage
# block below instead, and carrying every one at four aggregations each
# would quadruple width for little signal.
_AGGREGATED_DAY_FEATURES = (
    "frac_good_trips",
    "median_buffer_coverage_50",
    "median_shape_coverage",
    "median_direction_margin",
    "median_route_id_agreement",
    "median_offset_m",
    "worst_contradiction_fraction",
    "max_excursion_m",
    "median_start_dist_stop_m",
    "median_end_dist_stop_m",
    "median_heading_consistency",
    "median_direction_agreement",
    "median_fare_stationary_fraction",
    "median_fare_near_stop_m",
    "frac_device_points_in_windows",
    "frac_device_moving_points_in_windows",
    "n_clearly_bad_trips",
)

_DICTIONARY_SQL = """
    WITH pair_sources AS (
        SELECT bus_id, device_id, count(DISTINCT origin) AS n_dictionary_sources
        FROM ml.bus_matching_candidate_pairs
        GROUP BY bus_id, device_id
    ),
    bus_devices AS (
        SELECT bus_id, count(DISTINCT device_id) AS n_dict_devices_for_bus
        FROM ml.bus_matching_candidate_pairs GROUP BY bus_id
    ),
    device_buses AS (
        SELECT device_id, count(DISTINCT bus_id) AS n_dict_buses_for_device
        FROM ml.bus_matching_candidate_pairs GROUP BY device_id
    )
    SELECT
        p.bus_id, p.device_id, p.n_dictionary_sources,
        coalesce(bd.n_dict_devices_for_bus, 0) AS n_dict_devices_for_bus,
        coalesce(db_.n_dict_buses_for_device, 0) AS n_dict_buses_for_device
    FROM pair_sources p
    LEFT JOIN bus_devices bd ON bd.bus_id = p.bus_id
    LEFT JOIN device_buses db_ ON db_.device_id = p.device_id;
"""

_BUS_RUNNING_DAYS_SQL = """
    SELECT bus_id, count(DISTINCT trip_date) AS n_days_bus_ran
    FROM ml.trip_validity_final WHERE is_valid GROUP BY bus_id;
"""


def _aggregate_day_features(day_features: pd.DataFrame) -> pd.DataFrame:
    """Collapse per-day rows to one row per pair, with robust aggregations."""
    usable = [c for c in _AGGREGATED_DAY_FEATURES if c in day_features.columns]
    grouped = day_features.groupby(["bus_id", "device_id"])

    agg = grouped[usable].agg(["median", "mean", "std"])
    agg.columns = [f"{col}__{stat}" for col, stat in agg.columns]

    # Consistency matters as much as level: a pair that looks good on
    # average but wildly variable day to day is weaker evidence than a
    # steady one, and std is what carries that.
    agg["n_days_with_features"] = grouped.size()
    agg["n_days_with_data"] = grouped["n_trips_with_data"].apply(
        lambda s: (s > 0).sum()
    )
    return agg.reset_index()


def _day_model_score_aggregates(
    day_features: pd.DataFrame, day_scores: pd.Series
) -> pd.DataFrame:
    """Aggregate the day model's own P(match) across each pair's days.

    The single most informative input: it is the whole day-level model
    (31 features, trained on real trip labels) reduced to one number per
    day, then summarized over the month.
    """
    scored = day_features[["bus_id", "device_id"]].copy()
    scored["day_score"] = np.asarray(day_scores, dtype=np.float64)
    grouped = scored.groupby(["bus_id", "device_id"])["day_score"]
    out = grouped.agg(
        day_score_median="median",
        day_score_mean="mean",
        day_score_max="max",
        day_score_min="min",
        day_score_std="std",
    ).reset_index()
    out["day_score_frac_above_half"] = (
        scored.assign(hit=scored["day_score"] >= DAY_SCORE_HIT_THRESHOLD)
        .groupby(["bus_id", "device_id"])["hit"]
        .mean()
        .to_numpy()
    )
    return out


def _competition_features(pair_scores: pd.DataFrame) -> pd.DataFrame:
    """How this pair stacks up against its rivals, on both sides.

    Replaces `belief.py`'s hand-weighted cross-bus suppression with
    plain features: a device that is some *other* bus's clear favourite
    is weaker evidence here, and the model learns how much weaker
    rather than being told via `CROSS_SUPPRESSION_STRENGTH`.
    """
    out = pair_scores.copy()
    by_bus = out.groupby("bus_id")["day_score_mean"]
    out["bus_best_score"] = by_bus.transform("max")
    out["bus_rank"] = by_bus.rank(ascending=False, method="min")
    out["score_gap_to_bus_best"] = out["bus_best_score"] - out["day_score_mean"]
    out["n_candidates_for_bus"] = by_bus.transform("size")

    by_device = out.groupby("device_id")["day_score_mean"]
    out["device_best_score"] = by_device.transform("max")
    out["device_rank"] = by_device.rank(ascending=False, method="min")
    out["score_gap_to_device_best"] = out["device_best_score"] - out["day_score_mean"]
    out["n_buses_claiming_device"] = by_device.transform("size")

    # Mutual-best is the single cleanest structural signal available
    # without running an assignment: this bus's favourite device also
    # has this bus as its own favourite.
    out["is_mutual_best"] = ((out["bus_rank"] == 1) & (out["device_rank"] == 1)).astype(
        int
    )
    return out.drop(columns=["bus_best_score", "device_best_score"])


def build_pair_features(
    conn: psycopg.Connection,
    day_features: pd.DataFrame,
    day_scores: pd.Series,
) -> pd.DataFrame:
    """Build the full month-level feature table, one row per candidate pair.

    Args:
        conn: An open connection (for the dictionary and bus-schedule
            lookups).
        day_features: Concatenated `features_v2` rows for every date.
        day_scores: The day model's calibrated P(match) for each row of
            `day_features`, same order and length.

    Returns:
        One row per `(bus_id, device_id)` with every pair-level feature.
        Columns `bus_id`/`device_id` identify the pair; everything else
        is a model input.

    """
    agg = _aggregate_day_features(day_features)
    scores = _day_model_score_aggregates(day_features, day_scores)
    pairs = agg.merge(scores, on=["bus_id", "device_id"], how="left")
    pairs = _competition_features(pairs)

    dictionary = pd.read_sql(_DICTIONARY_SQL, conn)
    pairs = pairs.merge(dictionary, on=["bus_id", "device_id"], how="left")
    for col in (
        "n_dictionary_sources",
        "n_dict_devices_for_bus",
        "n_dict_buses_for_device",
    ):
        pairs[col] = pairs[col].fillna(0).astype(int)
    # A bus whose dictionaries name several different devices is exactly
    # the rare real device-swap case; a device named for several buses
    # likewise. Both make any single dictionary hit weaker evidence --
    # expressed as features so the model prices them, rather than via
    # the old hand-set DICTIONARY_CONFLICT_DAMPING.
    pairs["bus_has_dictionary_conflict"] = (pairs["n_dict_devices_for_bus"] > 1).astype(
        int
    )
    pairs["device_has_dictionary_conflict"] = (
        pairs["n_dict_buses_for_device"] > 1
    ).astype(int)

    running = pd.read_sql(_BUS_RUNNING_DAYS_SQL, conn)
    pairs = pairs.merge(running, on="bus_id", how="left")
    pairs["n_days_bus_ran"] = pairs["n_days_bus_ran"].fillna(0).astype(int)
    pairs["frac_bus_days_with_data"] = np.where(
        pairs["n_days_bus_ran"] > 0,
        pairs["n_days_with_data"] / pairs["n_days_bus_ran"],
        np.nan,
    )
    return pairs


def pair_feature_names(pairs: pd.DataFrame) -> list[str]:
    """Every model-input column in a `build_pair_features` frame."""
    return [c for c in pairs.columns if c not in {"bus_id", "device_id"}]
