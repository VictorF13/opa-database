"""Probabilistic per-bus-date belief over candidates, updated from votes.

Replaces the earlier hard "2 agreeing votes = resolved" rule with a
continuous belief: each trip-level vote is treated as noisy evidence
(never certain), starting from a prior seeded by a `score` column --
the Tier 1 cold-start heuristic (`frac_good_trips`) until a model
exists, then the trained model's own calibrated probability once
`db.compute_candidate_scores` has one to use (see that function's
docstring). A bus-date resolves once its top option's
posterior clears a confidence bar *and* is clearly separated from the
runner-up -- one bad vote just gets outweighed by more evidence rather
than overridden by exactly one more matching vote, and a string of
votes that keep disagreeing simply never resolves.

**Cross-bus-date suppression**: only one device can really be on one
bus at a time. If a device's posterior is high on one bus-date, its
posterior on every *other* bus-date that lists it as a candidate that
same day gets discounted before the final normalization -- this is
what lets confirming device D on bus B also help rule D out everywhere
else that date, without waiting for the full batch assignment (plan
Section 9, still a separate periodic job, not replicated here).

This is a single-pass heuristic, not an iterated joint solve: cheap
enough to run live in the labeling UI, at the cost of not being exactly
optimal like a proper `linear_sum_assignment` over the whole date.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

NONE_OPTION = "__none_of_these__"

# How strongly one "match"/"none_of_these" vote moves belief: picked
# option's log-odds go up by log(LR_FOR), every other option's log-odds
# go down by log(LR_AGAINST). Deliberately not overwhelming (LR_FOR=9
# means one vote alone can take a 50/50 prior to ~90/10, not to
# certainty) -- a second, disagreeing vote can still pull it back.
LR_FOR = 9.0
LR_AGAINST = 1.3

# Prior odds for "none of these" when no votes exist yet -- lower than
# an average fresh candidate's neutral 1:1, since blocking is designed
# to usually include the true device among the candidates.
PRIOR_NONE_ODDS = 0.3

# The Tier 1 cold-start score can be extremely decisive on its own
# (confirmed live: a clean candidate can score ~0.99 posterior from the
# prior alone, before any vote) -- which would let "resolved" happen
# without real human evidence, exactly what MIN_VOTES_TO_RESOLVE is
# supposed to prevent. Damping the prior's log-odds by this factor
# means even a maximal prior is worth less than a single vote
# (log(9) ~ 2.2 vs a damped max prior of ~0.6), so votes are what
# actually drive resolution; the undamped score still ranks selection
# (a bus-date the automation is already confident about is a good
# "quick confirm" candidate either way).
PRIOR_WEIGHT = 0.15

# A trained model's calibrated probability is not the same kind of
# thing as the static heuristic -- it's fit on real human-verified
# labels, not a fixed threshold rule, so it earns more trust. Damping
# it the same as the heuristic would mean confident model predictions
# could never count toward "confident_no_votes" coverage either,
# defeating the point of training a model at all (the coverage number
# would stay near zero forever, same as before a model existed).
#
# But a *fixed* weight is exactly what let a barely-trained model swing
# the whole system: confirmed live, with ~20-30 resolved bus-dates the
# calibration split has only 2-4 positives, and Platt scaling on that
# few examples is wildly unstable retrain to retrain (intercept swung
# from +4.98 to -0.87 between two consecutive retrains, pushing
# "confident coverage" from 26,000+ down to 30 and back).
#
# Ramping by raw label count alone is a blunt proxy, though -- it says
# nothing about whether the model is *actually* well-calibrated, just
# that time has passed. Tying the weight to `test_ece` (expected
# calibration error, already computed every retrain -- see `metrics.py`)
# instead means trust tracks a real, measured quality signal: a model
# with excellent raw discrimination but a still-shaky calibration layer
# earns less trust than one that's demonstrably well-calibrated, not
# just "has existed longer." `MODEL_TRUST_MIN_RESOLVED` is a floor
# underneath that -- ECE measured on a 2-3-row test set is itself not
# trustworthy, so below this many resolved bus-dates the weight is zero
# regardless of how good ECE happens to look.
MODEL_PRIOR_WEIGHT_CEILING = 0.6
MODEL_TRUST_MIN_RESOLVED = 15
ECE_FOR_ZERO_TRUST = 0.3
ECE_FOR_FULL_TRUST = 0.05


def model_prior_weight_for(
    n_resolved_bus_dates: int | None, test_ece: float | None
) -> float:
    """Scale `MODEL_PRIOR_WEIGHT_CEILING` by the model's measured calibration quality.

    Args:
        n_resolved_bus_dates: Resolved-bus-date count recorded at the
            model's own retrain time (`db.fetch_latest_model_run`'s
            `n_resolved_bus_dates`), or `None`/0 if unknown.
        test_ece: That same retrain's `test_ece` (lower is better --
            0 is perfect calibration). `None`/`NaN` (empty test split)
            is treated as untrustworthy.

    Returns:
        `0.0` below `MODEL_TRUST_MIN_RESOLVED` or with no measured ECE.
        Otherwise linearly interpolates from `0.0` at
        `ECE_FOR_ZERO_TRUST` up to `MODEL_PRIOR_WEIGHT_CEILING` at
        `ECE_FOR_FULL_TRUST` or better, clipped to that range.

    """
    if not n_resolved_bus_dates or n_resolved_bus_dates < MODEL_TRUST_MIN_RESOLVED:
        return 0.0
    if test_ece is None or (isinstance(test_ece, float) and np.isnan(test_ece)):
        return 0.0
    quality = (ECE_FOR_ZERO_TRUST - test_ece) / (
        ECE_FOR_ZERO_TRUST - ECE_FOR_FULL_TRUST
    )
    return MODEL_PRIOR_WEIGHT_CEILING * float(np.clip(quality, 0.0, 1.0))


# Plan Section 8's "temporal propagation": a pair confirmed as a match
# on one date raises that same pair's prior on *other* dates -- buses
# don't swap trackers daily. Deliberately not as strong as a real vote
# (LR_FOR=9) since it's indirect evidence, and deliberately never
# allowed to resolve a bus-date by itself (MIN_VOTES_TO_RESOLVE still
# requires a real vote) -- see `db.fetch_belief_summary` for how this
# stays acyclic: confirmed pairs are derived from a pass with this
# boost turned *off*, never from a boosted pass, so a pair can never
# boost itself into existence.
TEMPORAL_PRIOR_WEIGHT = 0.5
TEMPORAL_BOOST_ODDS = 5.0

CROSS_SUPPRESSION_STRENGTH = 3.0
CROSS_SUPPRESSION_THRESHOLD = 0.6

CONFIDENCE_THRESHOLD = 0.85
MARGIN_THRESHOLD = 0.3
MIN_VOTES_TO_RESOLVE = 1

TOP_RANK = 1
RUNNER_UP_RANK = 2


def _softmax_within_groups(
    df: pd.DataFrame, group_cols: list[str], score_col: str
) -> pd.Series:
    """Softmax `score_col` within each `group_cols` group, fully vectorized.

    `groupby(...).transform(lambda s: ...)` with a custom callback hits
    pandas' slow per-group Python path (confirmed live: ~30s for 40,007
    groups, rebuilding a MultiIndex per group). Built-in `.transform("max"
    "/"sum")` aggregation names use pandas' Cython fast path instead --
    same math, orders of magnitude faster.
    """
    group_max = df.groupby(group_cols)[score_col].transform("max")
    exp_score = np.exp(df[score_col] - group_max)
    group_sum = exp_score.groupby([df[c] for c in group_cols]).transform("sum")
    return exp_score / group_sum


def _prior_odds(score: pd.Series) -> pd.Series:
    """Map a 0-1 prior score to odds, neutral (1:1) if unscored."""
    clipped = score.clip(0.02, 0.98)
    odds = clipped / (1 - clipped)
    return odds.fillna(1.0)


def compute_raw_beliefs(
    candidates: pd.DataFrame,
    votes: pd.DataFrame,
    model_prior_weight: float = MODEL_PRIOR_WEIGHT_CEILING,
) -> pd.DataFrame:
    """Compute per-bus-date belief from prior + temporal + vote evidence only.

    This is `compute_date_beliefs` *without* cross-bus-date suppression --
    the raw, independent-per-bus-date posterior. Plan Section 9's global
    assignment is the real joint solve that supersedes the suppression
    heuristic, so it consumes *this* function's output (specifically
    `log_odds`, as `cost = -log_odds`), not the suppressed one --
    layering a global solve on top of an already-suppressed posterior
    would double-count the same "one device, one bus" constraint two
    different ways.

    Args:
        candidates: Columns `bus_id`, `date`, `device_id`, `score` (a 0-1
            prior -- `db.compute_candidate_scores`'s output: the trained
            model's calibrated probability where available, else the
            Tier 1 heuristic; may be `NaN` if unscored), `is_model_score`
            (bool -- whether `score` came from the trained model, which
            earns `model_prior_weight` trust, vs the static heuristic at
            `PRIOR_WEIGHT`), optionally `has_temporal_support` (bool --
            whether this exact `(bus_id, device_id)` pair was confirmed a
            match on a *different* date; adds `TEMPORAL_PRIOR_WEIGHT *
            log(TEMPORAL_BOOST_ODDS)` to this row's log-odds, on top of
            whichever score-based prior applies; missing/absent treated
            as all `False`) -- every candidate for every bus-date on this
            date.
        votes: Columns `bus_id`, `date`, `device_id`, `decision` --
            every trip-level vote recorded so far for bus-dates on this
            date (`decision` in `"match"`/`"none_of_these"`; `"unsure"`
            rows should already be filtered out by the caller since they
            carry no evidence).
        model_prior_weight: How much to trust `is_model_score` rows --
            see `model_prior_weight_for`. Defaults to full trust for
            direct/test callers that don't ramp it themselves.

    Returns:
        One row per `(bus_id, date, option)` (`option` is a `device_id`
        or the `NONE_OPTION` sentinel), columns `log_odds`, `posterior`
        (sums to 1 within each bus-date, *not* suppressed), `n_votes`
        (total votes for that bus-date, repeated per row).

    """
    if candidates.empty:
        return pd.DataFrame(
            columns=["bus_id", "date", "option", "log_odds", "posterior", "n_votes"]
        )

    is_model_score = candidates.get("is_model_score")
    if is_model_score is None:
        is_model_score = pd.Series(data=False, index=candidates.index)
    prior_weight = np.where(is_model_score.to_numpy(), model_prior_weight, PRIOR_WEIGHT)

    rows = candidates[["bus_id", "date", "device_id"]].rename(
        columns={"device_id": "option"}
    )
    rows["log_odds"] = prior_weight * np.log(
        _prior_odds(candidates["score"]).to_numpy()
    )

    has_temporal_support = candidates.get("has_temporal_support")
    if has_temporal_support is not None:
        rows["log_odds"] += np.where(
            has_temporal_support.to_numpy(),
            TEMPORAL_PRIOR_WEIGHT * np.log(TEMPORAL_BOOST_ODDS),
            0.0,
        )

    none_rows = (
        candidates[["bus_id", "date"]]
        .drop_duplicates()
        .assign(option=NONE_OPTION, log_odds=PRIOR_WEIGHT * np.log(PRIOR_NONE_ODDS))
    )
    all_rows = pd.concat([rows, none_rows], ignore_index=True)

    vote_counts = (
        votes.groupby(["bus_id", "date"]).size().rename("n_votes").reset_index()
        if not votes.empty
        else pd.DataFrame(columns=["bus_id", "date", "n_votes"])
    )

    if not votes.empty:
        picked = votes.assign(
            option=votes.apply(
                lambda r: r["device_id"] if r["decision"] == "match" else NONE_OPTION,
                axis=1,
            )
        )
        for_evidence = (
            picked.groupby(["bus_id", "date", "option"])
            .size()
            .rename("n_for")
            .reset_index()
        )
        all_rows = all_rows.merge(
            for_evidence, on=["bus_id", "date", "option"], how="left"
        )
        all_rows["n_for"] = all_rows["n_for"].fillna(0)
        all_rows = all_rows.merge(vote_counts, on=["bus_id", "date"], how="left")
        all_rows["n_votes"] = all_rows["n_votes"].fillna(0)
        n_against = all_rows["n_votes"] - all_rows["n_for"]
        all_rows["log_odds"] += all_rows["n_for"] * np.log(LR_FOR)
        all_rows["log_odds"] += n_against * np.log(1 / LR_AGAINST)
    else:
        all_rows["n_votes"] = 0

    all_rows["posterior"] = _softmax_within_groups(
        all_rows, ["bus_id", "date"], "log_odds"
    )
    return all_rows[["bus_id", "date", "option", "log_odds", "posterior", "n_votes"]]


def compute_date_beliefs(
    candidates: pd.DataFrame,
    votes: pd.DataFrame,
    model_prior_weight: float = MODEL_PRIOR_WEIGHT_CEILING,
) -> pd.DataFrame:
    """Compute posterior belief over every bus-date's candidates, for one date's data.

    Args:
        candidates: See `compute_raw_beliefs`.
        votes: See `compute_raw_beliefs`.
        model_prior_weight: See `compute_raw_beliefs`.

    Returns:
        One row per `(bus_id, date, option)` (`option` is a `device_id`
        or the `NONE_OPTION` sentinel), columns `posterior` (sums to 1
        within each bus-date), `n_votes` (total votes for that
        bus-date, repeated per row), `rank` (1 = top option within its
        bus-date).

    """
    if candidates.empty:
        return pd.DataFrame(
            columns=["bus_id", "date", "option", "posterior", "n_votes", "rank"]
        )

    all_rows = compute_raw_beliefs(candidates, votes, model_prior_weight)

    # Cross-bus-date suppression: for each device, its posterior on a
    # given bus-date gets discounted by how confidently it's already
    # claimed on a *different* bus-date the same day.
    device_rows = all_rows[all_rows["option"] != NONE_OPTION].copy()
    if not device_rows.empty:
        # Vectorized "this device's best posterior on a *different*
        # bus-date the same day": rank each device's posteriors within
        # (date, option), take the top-2, then for each row use the
        # runner-up if this row itself is the top one, else the top.
        # (Row-wise .apply() here was the actual bottleneck -- confirmed
        # live at ~30s for the full month; this vectorized version is
        # the fix.)
        ranked = device_rows.sort_values("posterior", ascending=False).copy()
        ranked["_rk"] = ranked.groupby(["date", "option"]).cumcount() + 1
        rank_cols = ["date", "option", "posterior"]
        top1 = ranked.loc[ranked["_rk"] == TOP_RANK, rank_cols].rename(
            columns={"posterior": "_top1"}
        )
        top2 = ranked.loc[ranked["_rk"] == RUNNER_UP_RANK, rank_cols].rename(
            columns={"posterior": "_top2"}
        )
        device_rows = device_rows.merge(top1, on=["date", "option"], how="left").merge(
            top2, on=["date", "option"], how="left"
        )
        device_rows["_top2"] = device_rows["_top2"].fillna(0.0)
        is_top = device_rows["posterior"] >= device_rows["_top1"] - 1e-12
        device_rows["elsewhere_max"] = np.where(
            is_top, device_rows["_top2"], device_rows["_top1"]
        )
        device_rows = device_rows.drop(columns=["_top1", "_top2"])
        suppress = device_rows["elsewhere_max"] > CROSS_SUPPRESSION_THRESHOLD
        device_rows.loc[suppress, "log_odds"] -= (
            device_rows.loc[suppress, "elsewhere_max"] * CROSS_SUPPRESSION_STRENGTH
        )

        none_rows_final = all_rows[all_rows["option"] == NONE_OPTION]
        all_rows = pd.concat(
            [device_rows.drop(columns="elsewhere_max"), none_rows_final],
            ignore_index=True,
        )
        all_rows["posterior"] = _softmax_within_groups(
            all_rows, ["bus_id", "date"], "log_odds"
        )

    all_rows["rank"] = all_rows.groupby(["bus_id", "date"])["posterior"].rank(
        ascending=False, method="first"
    )
    return all_rows[["bus_id", "date", "option", "posterior", "n_votes", "rank"]]


def summarize_bus_dates(beliefs: pd.DataFrame) -> pd.DataFrame:
    """Reduce per-option beliefs to one row per bus-date: top pick, margin, resolved.

    Args:
        beliefs: `compute_date_beliefs` output.

    Returns:
        Columns `bus_id`, `date`, `top_option`, `top_posterior`, `margin`
        (top minus runner-up posterior), `n_votes`, `resolved` (bool --
        the training-data signal, requires at least
        `MIN_VOTES_TO_RESOLVE` real human votes), `confident_no_votes`
        (bool -- the *coverage* signal: would this bus-date already
        clear the same confidence/margin bar from the prior alone, with
        zero votes? This is what should approach full coverage as the
        model improves, without a human voting on every one of
        thousands of bus-dates -- `resolved` alone was being displayed
        as if it were this number, which was misleading), `decision`
        ("match"/"none_of_these", only meaningful if `resolved`),
        `device_id` (only set if resolved as "match").

    """
    if beliefs.empty:
        return pd.DataFrame(
            columns=[
                "bus_id",
                "date",
                "top_option",
                "top_posterior",
                "margin",
                "n_votes",
                "resolved",
                "confident_no_votes",
                "decision",
                "device_id",
            ]
        )

    top = beliefs[beliefs["rank"] == TOP_RANK].drop_duplicates(["bus_id", "date"])
    runner_up = beliefs[beliefs["rank"] == RUNNER_UP_RANK][
        ["bus_id", "date", "posterior"]
    ].rename(columns={"posterior": "runner_up_posterior"})
    summary = top.merge(runner_up, on=["bus_id", "date"], how="left")
    summary["runner_up_posterior"] = summary["runner_up_posterior"].fillna(0.0)
    summary["margin"] = summary["posterior"] - summary["runner_up_posterior"]
    summary["confident_no_votes"] = (summary["posterior"] >= CONFIDENCE_THRESHOLD) & (
        summary["margin"] >= MARGIN_THRESHOLD
    )
    summary["resolved"] = summary["confident_no_votes"] & (
        summary["n_votes"] >= MIN_VOTES_TO_RESOLVE
    )
    summary["decision"] = np.where(
        summary["option"] == NONE_OPTION, "none_of_these", "match"
    )
    summary["device_id"] = np.where(
        summary["option"] == NONE_OPTION, None, summary["option"]
    )
    return summary.rename(
        columns={"posterior": "top_posterior", "option": "top_option"}
    )[
        [
            "bus_id",
            "date",
            "top_option",
            "top_posterior",
            "margin",
            "n_votes",
            "resolved",
            "confident_no_votes",
            "decision",
            "device_id",
        ]
    ]
