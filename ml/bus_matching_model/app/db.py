"""Database + feature-file access for the Bus Matching active learning app.

Reads `ml.bus_matching_candidates`, `ml.bus_matching_contestedness`,
`ml.trip_validity_final`, `ml.trip_validity_fares_final`,
`ml.trip_validity_route_shapes`, and `ml.bus_matching_avl_positions` --
all read-only, all built by earlier notebooks. Writes only to this app's
own `ml.bus_matching_trip_labels`.

Tier 1 features are read from the Parquet checkpoints in
`artifacts/features/`, not a database table (per the plan: the active
learning loop must never recompute features, and a month of per-pair
rows is cheap to hold in memory). The full-month background build can
still be in progress when this app runs -- callers should treat a
bus-date with no feature row as "not yet scored", not an error.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import belief
import pandas as pd
import psycopg
import training
from features import select_sample_trips as _select_sample_trips

from opa_database.config import settings

if TYPE_CHECKING:
    import datetime
    from collections.abc import Collection

Decision = Literal["match", "none_of_these", "unsure"]
Mode = Literal["uncontested", "contested"]
SelectionMode = Literal["hardest", "random"]

FEATURES_DIR = Path(__file__).resolve().parents[1] / "artifacts" / "features"

# stopping_signal() thresholds -- see its docstring.
MIN_RUNS_FOR_RATE = 2
MIN_RUNS_FOR_TREND = 3
GROWTH_SLOWDOWN_FACTOR = 0.7
BRIER_EPSILON = 1e-9
BRIER_PLATEAU_TOLERANCE = 0.05
CHECK_FEATURES_RESOLVED_THRESHOLD = 400


def get_connection() -> psycopg.Connection:
    """Open a new autocommit connection to the database.

    Returns:
        An open connection, in autocommit mode so a single dropped query
        can't leave the interactive Streamlit session's shared
        connection stuck mid-transaction.

    """
    conn = psycopg.connect(settings.db_dsn)
    conn.autocommit = True
    return conn


def load_available_features() -> pd.DataFrame:
    """Load every Tier 1 feature parquet checkpoint written so far.

    Returns:
        Concatenated frame across all `date=*.parquet` files currently on
        disk, columns `bus_id`, `device_id`, `date`, plus every Tier 1
        feature. Empty frame if the background build hasn't written
        anything yet.

    """
    paths = sorted(FEATURES_DIR.glob("date=*.parquet"))
    frames = [pd.read_parquet(p) for p in paths]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def trip_label_counts(conn: psycopg.Connection) -> dict[str, int]:
    """Count individual trip-level labels by decision.

    Args:
        conn: An open connection.

    Returns:
        Counts keyed by "match", "none_of_these", "unsure", "total".
        This is raw click volume, not resolved bus-dates -- see
        `fetch_resolved_labels` for the count that actually feeds
        training.

    """
    counts = {"match": 0, "none_of_these": 0, "unsure": 0}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT decision, count(*) FROM ml.bus_matching_trip_labels "
            "GROUP BY decision;"
        )
        counts.update(dict(cur.fetchall()))
    counts["total"] = sum(counts.values())
    return counts


def labeled_trip_ids(conn: psycopg.Connection) -> set[int]:
    """Every `trip_id` that already has a trip-level label.

    Args:
        conn: An open connection.

    Returns:
        Set of `trip_id`s -- used to avoid re-showing an already-decided
        trip.

    """
    with conn.cursor() as cur:
        cur.execute("SELECT trip_id FROM ml.bus_matching_trip_labels;")
        return {row[0] for row in cur.fetchall()}


def fetch_all_candidates_with_scores(
    conn: psycopg.Connection, features: pd.DataFrame
) -> pd.DataFrame:
    """Every candidate for every bus-date, joined to its Tier 1 score.

    Args:
        conn: An open connection.
        features: `load_available_features()` output.

    Returns:
        Columns `bus_id`, `date`, `device_id`, `from_dictionary`,
        `n_dictionary_sources` (0/1/2 -- how many of the two dictionary
        tables independently name this exact `(bus_id, device_id)`
        pair; corroboration by both is stronger evidence than either
        alone), `bus_has_dictionary_conflict` (bool -- this bus has
        *multiple* distinct dictionary-sourced devices across all its
        candidates, so any single dictionary pair for it is weaker
        evidence, on request), `frac_good_trips` (`NaN` if not yet
        featurized).

    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH pair_sources AS (
                SELECT bus_id, device_id, count(DISTINCT origin) AS n_sources
                FROM ml.bus_matching_candidate_pairs
                GROUP BY bus_id, device_id
            ),
            bus_distinct_devices AS (
                SELECT bus_id, count(DISTINCT device_id) AS n_distinct_devices
                FROM ml.bus_matching_candidate_pairs
                GROUP BY bus_id
            )
            SELECT
                c.bus_id, c.date, c.device_id, c.from_dictionary,
                coalesce(ps.n_sources, 0) AS n_dictionary_sources,
                coalesce(bd.n_distinct_devices, 0) > 1 AS bus_has_dictionary_conflict
            FROM ml.bus_matching_candidates c
            LEFT JOIN pair_sources ps
              ON ps.bus_id = c.bus_id AND ps.device_id = c.device_id
            LEFT JOIN bus_distinct_devices bd ON bd.bus_id = c.bus_id;
            """
        )
        cand = pd.DataFrame.from_records(
            cur.fetchall(),
            columns=[
                "bus_id",
                "date",
                "device_id",
                "from_dictionary",
                "n_dictionary_sources",
                "bus_has_dictionary_conflict",
            ],
        )
    if features.empty:
        cand["frac_good_trips"] = float("nan")
        return cand
    return cand.merge(
        features[["bus_id", "device_id", "date", "frac_good_trips"]],
        on=["bus_id", "device_id", "date"],
        how="left",
    )


def compute_candidate_scores(
    conn: psycopg.Connection,
    features: pd.DataFrame,
    model_state: dict[str, Any] | None,
) -> pd.DataFrame:
    """Score every candidate: model's P(match) if available, else the Tier 1 heuristic.

    This is the "feed the model back into belief" step: once
    `model_state` (from `registry.load_run`/a fresh `_run_training_cycle`)
    exists, its calibrated probability is a genuinely better prior than
    the static Tier 1 score, since it's fit on real labels instead of a
    fixed threshold rule. Falls back to `frac_good_trips` per-row
    whenever the model's own feature columns aren't available for that
    row (not yet featurized) or no model exists yet at all.

    Args:
        conn: An open connection.
        features: `load_available_features()` output.
        model_state: `{"model", "calibrator", "selected_features"}`, or
            `None` before the first retrain.

    Returns:
        Columns `bus_id`, `date`, `device_id`, `from_dictionary`, `score`
        (0-1, `NaN` if neither source is available for that row),
        `is_model_score` (bool -- whether `score` came from the model, so
        `belief.py` knows to trust it more than the heuristic).

    """
    cand = fetch_all_candidates_with_scores(conn, features)
    cand = cand.rename(columns={"frac_good_trips": "score"})
    cand["is_model_score"] = False
    out_cols = [
        "bus_id",
        "date",
        "device_id",
        "from_dictionary",
        "n_dictionary_sources",
        "bus_has_dictionary_conflict",
        "score",
        "is_model_score",
    ]
    if model_state is None or features.empty:
        return cand[out_cols]

    selected = model_state["selected_features"]
    merged = cand.merge(
        features[["bus_id", "device_id", "date", *selected]],
        on=["bus_id", "device_id", "date"],
        how="left",
    )
    has_features = merged[selected].notna().all(axis=1)
    if has_features.any():
        raw = training.predict_positive_proba(
            model_state["model"], merged.loc[has_features, selected]
        )
        merged.loc[has_features, "score"] = model_state["calibrator"].predict(raw)
        merged.loc[has_features, "is_model_score"] = True
    return merged[out_cols]


def fetch_all_votes(conn: psycopg.Connection) -> pd.DataFrame:
    """Every non-"unsure" trip-level vote recorded so far.

    Args:
        conn: An open connection.

    Returns:
        Columns `bus_id`, `date`, `device_id`, `decision`.

    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT bus_id, date, device_id, decision FROM ml.bus_matching_trip_labels "
            "WHERE decision <> 'unsure';"
        )
        return pd.DataFrame.from_records(
            cur.fetchall(), columns=["bus_id", "date", "device_id", "decision"]
        )


def _add_temporal_support(
    candidates: pd.DataFrame, confirmed_pairs: pd.DataFrame
) -> pd.DataFrame:
    """Flag candidates whose `(bus_id, device_id)` pair was confirmed on another date.

    Fully vectorized (merge + groupby-any, no row-wise `.apply`) --
    confirmed at this scale (tens of thousands of candidate rows) the
    row-wise version was slow enough to matter.

    Args:
        candidates: Must have `bus_id`, `date`, `device_id`.
        confirmed_pairs: `bus_id`, `date`, `device_id` of pairs already
            confirmed a match (vote-only resolution, see
            `fetch_belief_summary`).

    Returns:
        `candidates` with an added `has_temporal_support` bool column.

    """
    candidates = candidates.copy()
    if confirmed_pairs.empty:
        candidates["has_temporal_support"] = False
        return candidates
    merged = candidates.merge(
        confirmed_pairs.rename(columns={"date": "confirmed_date"}),
        on=["bus_id", "device_id"],
        how="left",
    )
    merged["_supported"] = merged["confirmed_date"].notna() & (
        merged["confirmed_date"] != merged["date"]
    )
    support = (
        merged.groupby(["bus_id", "date", "device_id"])["_supported"]
        .any()
        .reset_index(name="has_temporal_support")
    )
    return candidates.merge(support, on=["bus_id", "date", "device_id"], how="left")


def fetch_belief_summary(
    conn: psycopg.Connection,
    features: pd.DataFrame,
    model_state: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Compute the current probabilistic belief for every bus-date -- see `belief.py`.

    Two passes to add the temporal-support prior (plan Section 8)
    without circularity: pass 1 computes belief *without* any temporal
    boost and reads off which pairs are already vote-confirmed matches;
    pass 2 re-scores every candidate with that boost applied and
    returns *that* result. A pair can never boost itself into
    existence, since the confirmed-pairs list pass 1 produces never had
    the boost applied in the first place.

    Args:
        conn: An open connection.
        features: `load_available_features()` output.
        model_state: Passed through to `compute_candidate_scores` -- once
            a model exists, its calibrated probability drives the prior
            instead of the static Tier 1 heuristic.

    Returns:
        `belief.summarize_bus_dates` output: one row per bus-date with
        `top_option`, `top_posterior`, `margin`, `n_votes`, `resolved`,
        `decision`, `device_id`, plus `has_dictionary` (bool -- any
        candidate for this bus-date is dictionary-sourced) and
        `has_score` (bool -- any candidate has a real, non-`NaN` score;
        `False` means every candidate is missing GTFS/AVL data for
        every sampled trip, so there is nothing to visually compare --
        see `fetch_next_trip`).

    """
    candidates = compute_candidate_scores(conn, features, model_state)
    votes = fetch_all_votes(conn)

    run_row = (model_state or {}).get("run_row") or {}
    model_prior_weight = belief.model_prior_weight_for(
        run_row.get("n_resolved_bus_dates"), run_row.get("test_ece")
    )

    base_beliefs = belief.compute_date_beliefs(candidates, votes, model_prior_weight)
    base_summary = belief.summarize_bus_dates(base_beliefs)
    confirmed_pairs = base_summary[
        base_summary["resolved"] & (base_summary["decision"] == "match")
    ][["bus_id", "date", "device_id"]]

    candidates = _add_temporal_support(candidates, confirmed_pairs)
    beliefs = belief.compute_date_beliefs(candidates, votes, model_prior_weight)
    summary = belief.summarize_bus_dates(beliefs)

    flags = candidates.groupby(["bus_id", "date"]).agg(
        has_dictionary=("from_dictionary", "any"),
        has_score=("score", lambda s: bool(s.notna().any())),
    )
    return summary.merge(flags, on=["bus_id", "date"], how="left").fillna(
        {"has_dictionary": False, "has_score": False}
    )


def fetch_resolved_labels(
    conn: psycopg.Connection,
    features: pd.DataFrame,
    model_state: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Bus-dates whose belief has resolved -- see `belief.summarize_bus_dates`.

    Args:
        conn: An open connection.
        features: `load_available_features()` output.
        model_state: Passed through to `fetch_belief_summary`.

    Returns:
        Columns `bus_id`, `date`, `decision`, `device_id` -- one row per
        resolved bus-date only.

    """
    summary = fetch_belief_summary(conn, features, model_state)
    resolved = summary[summary["resolved"]]
    return resolved[["bus_id", "date", "decision", "device_id"]].reset_index(drop=True)


def resolved_bus_dates(
    conn: psycopg.Connection,
    features: pd.DataFrame,
    model_state: dict[str, Any] | None = None,
) -> set[tuple[str, Any]]:
    """Every `(bus_id, date)` that has already resolved -- see `fetch_resolved_labels`.

    Args:
        conn: An open connection.
        features: `load_available_features()` output.
        model_state: Passed through to `fetch_resolved_labels`.

    Returns:
        Set of `(bus_id, date)` tuples.

    """
    resolved = fetch_resolved_labels(conn, features, model_state)
    if resolved.empty:
        return set()
    return set(zip(resolved["bus_id"], resolved["date"], strict=True))


def total_bus_date_count(conn: psycopg.Connection) -> int:
    """Total distinct bus-dates with at least one candidate -- the coverage denominator.

    Args:
        conn: An open connection.

    Returns:
        Count of distinct `(bus_id, date)` pairs in `ml.bus_matching_candidates`.

    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(DISTINCT (bus_id, date)) FROM ml.bus_matching_candidates;"
        )
        row = cur.fetchone()
        return row[0] if row else 0


def model_coverage_count(
    conn: psycopg.Connection,
    features: pd.DataFrame,
    model_state: dict[str, Any] | None,
) -> int:
    """Count bus-dates the prior is *already* confident about, zero votes needed.

    This is the actual "how close to full coverage" number -- distinct
    from `resolved_bus_dates`, which requires real human votes and is
    meant to stay small (the plan's "few hundred" training-label
    budget). This one is what should climb toward `total_bus_date_count`
    as the model improves, without a human voting on every bus-date.

    Args:
        conn: An open connection.
        features: `load_available_features()` output.
        model_state: Passed through to `fetch_belief_summary`.

    Returns:
        Count of bus-dates with `confident_no_votes == True`.

    """
    summary = fetch_belief_summary(conn, features, model_state)
    if summary.empty:
        return 0
    return int(summary["confident_no_votes"].sum())


def stopping_signal(conn: psycopg.Connection) -> dict[str, Any]:
    """Auto-detect whether labeling looks like it's converging -- plan Section 11.

    Watches two things together across retrain history in
    `ml.bus_matching_model_runs`: whether the resolved-bus-date growth
    *rate* (resolved gained per trip-label spent) is slowing between the
    last two retrain intervals, and whether `test_brier` has stopped
    moving much across the last three retrains. Neither alone is
    conclusive (see the plan: coverage, margin, switch-count, and
    held-out agreement all need to plateau *together*) -- this covers
    the two signals cheaply computable from what's already tracked, not
    the full list.

    Args:
        conn: An open connection.

    Returns:
        Dict with `verdict` (one of `"too_early"`, `"keep_going"`,
        `"slowing"`, `"stop_soon"`, `"check_features"`), a human-readable
        `message`, and (when available) `n_resolved`/`recent_rate` for
        display.

    """
    runs = fetch_model_runs(conn)
    if "n_resolved_bus_dates" in runs.columns:
        runs = runs.dropna(subset=["n_resolved_bus_dates", "n_trip_labels_total"])
    else:
        runs = runs.iloc[0:0]

    if len(runs) < MIN_RUNS_FOR_RATE:
        return {
            "verdict": "too_early",
            "message": "Not enough retrains yet to see a trend -- keep labeling.",
        }

    latest, prev = runs.iloc[-1], runs.iloc[-2]
    resolved_gain = latest["n_resolved_bus_dates"] - prev["n_resolved_bus_dates"]
    label_cost = latest["n_trip_labels_total"] - prev["n_trip_labels_total"]
    recent_rate = resolved_gain / label_cost if label_cost else 0.0

    growth_slowing = False
    if len(runs) >= MIN_RUNS_FOR_TREND:
        prev2 = runs.iloc[-MIN_RUNS_FOR_TREND]
        earlier_gain = prev["n_resolved_bus_dates"] - prev2["n_resolved_bus_dates"]
        earlier_cost = prev["n_trip_labels_total"] - prev2["n_trip_labels_total"]
        earlier_rate = earlier_gain / earlier_cost if earlier_cost else 0.0
        growth_slowing = (
            earlier_rate > 0 and recent_rate < earlier_rate * GROWTH_SLOWDOWN_FACTOR
        )

    metric_plateau = False
    if len(runs) >= MIN_RUNS_FOR_TREND:
        last3 = runs["test_brier"].tail(MIN_RUNS_FOR_TREND)
        if last3.notna().all() and last3.iloc[0] > BRIER_EPSILON:
            rel_change = abs(last3.iloc[-1] - last3.iloc[0]) / last3.iloc[0]
            metric_plateau = rel_change < BRIER_PLATEAU_TOLERANCE

    n_resolved = int(latest["n_resolved_bus_dates"])
    if n_resolved > CHECK_FEATURES_RESOLVED_THRESHOLD and not (
        growth_slowing or metric_plateau
    ):
        verdict, message = (
            "check_features",
            f"{n_resolved} resolved and still climbing steeply past the plan's ~400 "
            "rough ceiling -- that usually points to a features/candidate-generation "
            'problem, not "label more."',
        )
    elif growth_slowing and metric_plateau:
        verdict, message = (
            "stop_soon",
            "Both growth rate and model metrics have plateaued across the last few "
            "retrains -- probably enough.",
        )
    elif growth_slowing or metric_plateau:
        verdict, message = (
            "slowing",
            "One signal (growth rate or metrics) is slowing but not both yet -- "
            "a bit more labeling may still help.",
        )
    else:
        verdict, message = "keep_going", "Still improving -- keep labeling."

    return {
        "verdict": verdict,
        "message": message,
        "n_resolved": n_resolved,
        "recent_rate": recent_rate,
    }


def fetch_next_trip(
    conn: psycopg.Connection,
    features: pd.DataFrame,
    *,
    model_state: dict[str, Any] | None = None,
    selection_mode: SelectionMode = "hardest",
    exclude_trip_ids: Collection[int] = (),
    exclude_bus_dates: Collection[tuple[str, Any]] = (),
    max_attempts: int = 50,
) -> tuple[str, Any, int, bool] | None:
    """Auto-select the single most informative next trip to label.

    Bus-dates that already have at least one vote and aren't resolved
    yet come first, ranked by highest posterior first (closest to
    crossing the confidence bar -- confirmed live this matters: with
    `MIN_VOTES_TO_RESOLVE` effectively needing ~2 real votes to clear
    `CONFIDENCE_THRESHOLD` from a single confirming vote alone, ranking
    purely by margin let 10 single votes scatter across 10 different
    bus-dates with zero resolutions, since a fresh zero-vote bus-date's
    margin can look just as "small" as a partially-voted one's). This
    part is unaffected by `selection_mode` -- finishing something
    already started is worth doing either way.

    Only once nothing is in progress does selection fall back to fresh
    zero-vote bus-dates. Both modes agree on one thing first: `has_score`
    descending -- a bus-date where every candidate is missing GTFS/AVL
    data for every sampled trip (`has_score=False`) has nothing to
    visually compare and no real evidence driving its prior either
    (`_prior_odds` treats a missing score as neutral 1:1 odds), so *all*
    its candidates look equally "uncertain" by margin alone -- that's
    absence of evidence masquerading as genuine ambiguity, not a hard
    case, and not a representative "normal" case either. Pushed last in
    both modes, on request: these may still get resolved indirectly
    later, via cross-suppression or temporal support from other
    confirmed dates, without ever needing a human look.

    From there the two modes diverge, on request (pure uncertainty
    sampling concentrates training data right at the model's current
    decision boundary and starves it of the "obviously easy" majority
    case -- confirmed live: the model's own decision boundary visibly
    jittered retrain to retrain, swinging live coverage by
    thousands of bus-dates even with `test_ece` stable and trust
    already at its ceiling):

    - `"hardest"` (default): among the analyzable remainder, tiebreak by
      `has_dictionary` descending (dictionary-backed candidates are
      safer/faster to label, on request), then `margin` ascending --
      closest call in the prior alone, per the plan's Section 7
      "contested pairs" priority.
    - `"random"`: among the analyzable remainder, still tiebreaks by
      `has_dictionary` descending first (dictionary-backed candidates
      stay safer/faster to label even when the point is to sample the
      ordinary case, on request) but shuffles uniformly at random
      *within* each of those two tiers instead of sorting by margin --
      deliberately ignores margin, so labeling sweeps up the
      ordinary/easy majority the hardest-first mode systematically
      skips, anchoring the model against boundary jitter, without
      giving up the dictionary safety net.

    No manual queue choice either way -- `selection_mode` toggles which
    automatic policy runs, it's still never a hand-picked queue.

    Args:
        conn: An open connection.
        features: `load_available_features()` output.
        model_state: Passed through to `fetch_belief_summary` -- once a
            model exists it drives the ranking's prior, same as
            resolution.
        selection_mode: `"hardest"` (default) for uncertainty sampling,
            `"random"` to sweep up ordinary/easy cases instead -- see
            above.
        exclude_trip_ids: Trip ids to treat as already decided (labeled
            this session, or skipped).
        exclude_bus_dates: `(bus_id, date)` pairs to skip entirely this
            session (the "get a different bus/route" escape hatch).
        max_attempts: How many top-ranked bus-dates to try before giving
            up -- guards against a pathological run of exhausted
            bus-dates (all sample trips already labeled/skipped, still
            unresolved) burning unbounded time.

    Returns:
        `(bus_id, date, trip_id, is_contested)`, or `None` if nothing is
        left to label.

    """
    summary = fetch_belief_summary(conn, features, model_state)
    unresolved = summary[~summary["resolved"]]
    in_progress = unresolved[unresolved["n_votes"] > 0].sort_values(
        "top_posterior", ascending=False
    )
    zero_vote = unresolved[unresolved["n_votes"] == 0]
    if selection_mode == "random":
        analyzable = zero_vote[zero_vote["has_score"]]
        unanalyzable = zero_vote[~zero_vote["has_score"]]
        dict_backed = analyzable[analyzable["has_dictionary"]].sample(frac=1.0)
        non_dict = analyzable[~analyzable["has_dictionary"]].sample(frac=1.0)
        fresh = pd.concat([dict_backed, non_dict, unanalyzable], ignore_index=True)
    else:
        fresh = zero_vote.sort_values(
            ["has_score", "has_dictionary", "margin"], ascending=[False, False, True]
        )
    unresolved = pd.concat([in_progress, fresh], ignore_index=True)
    if exclude_bus_dates:
        excluded_df = pd.DataFrame(list(exclude_bus_dates), columns=["bus_id", "date"])
        unresolved = unresolved.merge(
            excluded_df, on=["bus_id", "date"], how="left", indicator=True
        )
        unresolved = unresolved[unresolved["_merge"] == "left_only"].drop(
            columns="_merge"
        )
    if unresolved.empty:
        return None

    exclude = set(exclude_trip_ids) | labeled_trip_ids(conn)
    for row in unresolved.head(max_attempts).itertuples(index=False):
        trips = fetch_bus_date_trips(conn, row.bus_id, row.date)
        if trips.empty:
            continue
        sampled = _select_sample_trips(trips)
        remaining = sampled[~sampled["trip_id"].isin(exclude)]
        if remaining.empty:
            continue
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_contested FROM ml.bus_matching_contestedness "
                "WHERE bus_id = %(bus_id)s AND trip_date = %(date)s;",
                {"bus_id": row.bus_id, "date": row.date},
            )
            contested_row = cur.fetchone()
        is_contested = bool(contested_row[0]) if contested_row else True
        return row.bus_id, row.date, int(remaining.iloc[0]["trip_id"]), is_contested
    return None


def fetch_bus_date_candidates(
    conn: psycopg.Connection,
    features: pd.DataFrame,
    bus_id: str,
    date: datetime.date,
    *,
    model_state: dict[str, Any] | None = None,
    max_shown: int = 6,
) -> pd.DataFrame:
    """Every candidate device for one bus-date, joined to its best-available score.

    On request, shown directly to the labeler (score + dictionary-origin
    badge on each item), which trades away the plan's anti-bias hiding
    on purpose -- an informed, explicit choice, not an oversight.

    Args:
        conn: An open connection.
        features: `load_available_features()` output.
        bus_id: The bus.
        date: The trip date.
        model_state: If given, candidates with available features get
            the trained model's calibrated probability as `score`
            instead of the Tier 1 heuristic (`compute_candidate_scores`'
            per-bus-date-scoped equivalent).
        max_shown: Contested mode caps the number of candidates actually
            rendered (each adds a track + a chainage series) -- kept to
            the top `max_shown` by score; every candidate is still a
            selectable answer via "more candidates exist" being
            surfaced separately, not silently dropped from the label
            options.

    Returns:
        Columns `device_id`, `overlap_count`, `from_dictionary`,
        `from_blocking`, `score`, `is_model_score`,
        `median_buffer_coverage_50`, sorted by `score` descending
        (worst/unscored last), capped to `max_shown`.

    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT device_id, overlap_count, from_dictionary, from_blocking
            FROM ml.bus_matching_candidates
            WHERE bus_id = %(bus_id)s AND date = %(date)s;
            """,
            {"bus_id": bus_id, "date": date},
        )
        cand = pd.DataFrame.from_records(
            cur.fetchall(),
            columns=["device_id", "overlap_count", "from_dictionary", "from_blocking"],
        )

    if features.empty:
        cand["score"] = float("nan")
        cand["is_model_score"] = False
        cand["median_buffer_coverage_50"] = float("nan")
        return cand.sort_values("score", ascending=False, na_position="last").head(
            max_shown
        )

    today = features[(features["bus_id"] == bus_id) & (features["date"] == date)]
    cand = cand.merge(
        today[["device_id", "frac_good_trips", "median_buffer_coverage_50"]],
        on="device_id",
        how="left",
    )
    cand = cand.rename(columns={"frac_good_trips": "score"})
    cand["is_model_score"] = False

    if model_state is not None:
        selected = model_state["selected_features"]
        feat_cols = today[["device_id", *selected]]
        feat_merged = cand[["device_id"]].merge(feat_cols, on="device_id", how="left")
        has_features = feat_merged[selected].notna().all(axis=1)
        if has_features.any():
            raw = training.predict_positive_proba(
                model_state["model"], feat_merged.loc[has_features, selected]
            )
            calibrated = model_state["calibrator"].predict(raw)
            cand.loc[has_features.to_numpy(), "score"] = calibrated
            cand.loc[has_features.to_numpy(), "is_model_score"] = True

    cand = cand.sort_values("score", ascending=False, na_position="last")
    return cand.head(max_shown).reset_index(drop=True)


def fetch_bus_date_trips(
    conn: psycopg.Connection, bus_id: str, date: datetime.date
) -> pd.DataFrame:
    """Every valid trip for one bus-date, time-ordered.

    Args:
        conn: An open connection.
        bus_id: The bus.
        date: The trip date.

    Returns:
        Columns `trip_id`, `route_id`, `trip_start_timestamp`,
        `trip_end_timestamp`, `gtfs_feed_version_date`,
        `gtfs_shape_id_i`, `gtfs_shape_id_v`.

    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trip_id, route_id, trip_start_timestamp, trip_end_timestamp,
                   gtfs_feed_version_date, gtfs_shape_id_i, gtfs_shape_id_v
            FROM ml.trip_validity_final
            WHERE is_valid AND bus_id = %(bus_id)s AND trip_date = %(date)s
            ORDER BY trip_start_timestamp;
            """,
            {"bus_id": bus_id, "date": date},
        )
        return pd.DataFrame.from_records(
            cur.fetchall(),
            columns=[
                "trip_id",
                "route_id",
                "trip_start_timestamp",
                "trip_end_timestamp",
                "gtfs_feed_version_date",
                "gtfs_shape_id_i",
                "gtfs_shape_id_v",
            ],
        )


def fetch_shape_geojson(
    conn: psycopg.Connection, feed_version_date: datetime.date, shape_id: str | None
) -> list[list[float]] | None:
    """GTFS shape coordinates for one direction, or `None` if unmatched.

    Args:
        conn: An open connection.
        feed_version_date: The trip's GTFS feed snapshot.
        shape_id: `gtfs_shape_id_i` or `gtfs_shape_id_v`, possibly `None`.

    Returns:
        `[lon, lat]` coordinate list, or `None`.

    """
    if shape_id is None or (isinstance(shape_id, float) and pd.isna(shape_id)):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ST_AsGeoJSON(shape_geom) FROM ml.trip_validity_route_shapes "
            "WHERE feed_version_date = %(feed)s AND shape_id = %(shape_id)s;",
            {"feed": feed_version_date, "shape_id": shape_id},
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return json.loads(row[0])["coordinates"]


def fetch_route_stops(
    conn: psycopg.Connection, feed_version_date: datetime.date, shape_id: str | None
) -> pd.DataFrame:
    """GTFS stops along one shape, unioned across stop-sequence variants.

    Args:
        conn: An open connection.
        feed_version_date: The trip's GTFS feed snapshot.
        shape_id: The reference shape (see `db.fetch_shape_geojson`).

    Returns:
        Columns `stop_id`, `stop_lat`, `stop_lon`, deduplicated -- empty
        frame if `shape_id` is `None` or unmatched.

    """
    if shape_id is None or (isinstance(shape_id, float) and pd.isna(shape_id)):
        return pd.DataFrame(columns=["stop_id", "stop_lat", "stop_lon"])
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT stop_id, stop_lat, stop_lon
            FROM ml.trip_validity_route_stops
            WHERE feed_version_date = %(feed)s AND shape_id = %(shape_id)s;
            """,
            {"feed": feed_version_date, "shape_id": shape_id},
        )
        return pd.DataFrame.from_records(
            cur.fetchall(), columns=["stop_id", "stop_lat", "stop_lon"]
        )


def fetch_device_trip_positions(
    conn: psycopg.Connection,
    device_id: str,
    start_ts: datetime.datetime,
    end_ts: datetime.datetime,
) -> pd.DataFrame:
    """One device's AVL positions inside one trip's time window.

    Args:
        conn: An open connection.
        device_id: The candidate device.
        start_ts: Trip window start (timestamptz).
        end_ts: Trip window end (timestamptz).

    Returns:
        Columns `metric_timestamp`, `latitude`, `longitude`, `epoch`
        (seconds since epoch, float), `x`/`y` (metric CRS SRID 31984,
        matching the `gtfs_cache` shapes -- computed in SQL rather than
        client-side reprojection, so no new dependency is needed),
        ordered by time.

    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT metric_timestamp, latitude, longitude,
                   extract(epoch FROM metric_timestamp)::float8 AS epoch,
                   ST_X(ST_Transform(geom, 31984)) AS x,
                   ST_Y(ST_Transform(geom, 31984)) AS y
            FROM ml.bus_matching_avl_positions
            WHERE device_id = %(device_id)s
              AND metric_timestamp >= %(start)s AND metric_timestamp <= %(end)s
            ORDER BY metric_timestamp;
            """,
            {"device_id": device_id, "start": start_ts, "end": end_ts},
        )
        return pd.DataFrame.from_records(
            cur.fetchall(),
            columns=["metric_timestamp", "latitude", "longitude", "epoch", "x", "y"],
        )


def expand_labels_to_training_rows(
    conn: psycopg.Connection,
    features: pd.DataFrame,
    model_state: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Turn resolved bus-date verdicts into per-candidate binary training rows.

    Mirrors the plan's Section 8 immediate constraint: a resolved
    "match" on device D makes D a positive row and every *other*
    candidate shown for that bus-date a negative row; a resolved
    "none_of_these" makes every shown candidate a negative row with no
    positive. Only *resolved* bus-dates (see `fetch_resolved_labels`)
    contribute rows -- a bus-date with disagreeing or too-few trip votes
    isn't used for training yet.

    Args:
        conn: An open connection.
        features: `load_available_features()` output -- rows are dropped
            if their `(bus_id, device_id, date)` isn't featurized yet
            (the full-month background build may still be running).
        model_state: Passed through to `fetch_resolved_labels` -- the
            *previous* model (if any) is what determines which
            bus-dates are already resolved going into this retrain.

    Returns:
        Columns `bus_id`, `date`, `device_id`, `label` (bool), plus
        every `features.DAY_FEATURE_NAMES` column.

    """
    labels = fetch_resolved_labels(conn, features, model_state)
    if labels.empty or features.empty:
        return pd.DataFrame()

    with conn.cursor() as cur:
        cur.execute("SELECT bus_id, date, device_id FROM ml.bus_matching_candidates;")
        candidates = pd.DataFrame.from_records(
            cur.fetchall(), columns=["bus_id", "date", "device_id"]
        )

    rows = []
    for row in labels.itertuples(index=False):
        bus_date_candidates = candidates[
            (candidates["bus_id"] == row.bus_id) & (candidates["date"] == row.date)
        ]["device_id"]
        for candidate_device in bus_date_candidates:
            positive = row.decision == "match" and candidate_device == row.device_id
            rows.append(
                {
                    "bus_id": row.bus_id,
                    "date": row.date,
                    "device_id": candidate_device,
                    "label": positive,
                }
            )
    expanded = pd.DataFrame(rows)
    return expanded.merge(features, on=["bus_id", "device_id", "date"], how="inner")


def insert_model_run(conn: psycopg.Connection, run: dict[str, Any]) -> int:
    """Persist one training run's config, metrics, and artifact location.

    Args:
        conn: An open connection.
        run: Keys matching `ml.bus_matching_model_runs`'s columns.

    Returns:
        The new row's `run_id`.

    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ml.bus_matching_model_runs
                (run_type, n_train_labels, hyperparameters, selected_features,
                 calibration_params, cv_brier_score, test_auc, test_brier,
                 test_log_loss, test_ece, artifact_path,
                 n_resolved_bus_dates, n_trip_labels_total)
            VALUES (%(run_type)s, %(n_train_labels)s, %(hyperparameters)s,
                    %(selected_features)s, %(calibration_params)s, %(cv_brier_score)s,
                    %(test_auc)s, %(test_brier)s, %(test_log_loss)s, %(test_ece)s,
                    %(artifact_path)s, %(n_resolved_bus_dates)s,
                    %(n_trip_labels_total)s)
            RETURNING run_id;
            """,
            {
                "run_type": run["run_type"],
                "n_train_labels": run["n_train_labels"],
                "hyperparameters": json.dumps(run["hyperparameters"]),
                "selected_features": json.dumps(run["selected_features"]),
                "calibration_params": json.dumps(run["calibration_params"])
                if run.get("calibration_params") is not None
                else None,
                "cv_brier_score": run.get("cv_brier_score"),
                "test_auc": run.get("test_auc"),
                "test_brier": run.get("test_brier"),
                "test_log_loss": run.get("test_log_loss"),
                "test_ece": run.get("test_ece"),
                "artifact_path": run["artifact_path"],
                "n_resolved_bus_dates": run.get("n_resolved_bus_dates"),
                "n_trip_labels_total": run.get("n_trip_labels_total"),
            },
        )
        result = cur.fetchone()
        if result is None:
            msg = "INSERT ... RETURNING run_id unexpectedly returned no row"
            raise RuntimeError(msg)
        return result[0]


def fetch_latest_model_run(conn: psycopg.Connection) -> dict[str, Any] | None:
    """Fetch the most recent model run's full row.

    Args:
        conn: An open connection.

    Returns:
        A dict of column name to value, or `None` if no run exists yet.

    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM ml.bus_matching_model_runs ORDER BY created_at DESC LIMIT 1;"
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d.name for d in cur.description or []]
        return dict(zip(columns, row, strict=True))


def fetch_model_runs(conn: psycopg.Connection) -> pd.DataFrame:
    """Fetch every model run, oldest first, for charting metrics over time.

    Args:
        conn: An open connection.

    Returns:
        A frame with one row per run, columns matching
        `ml.bus_matching_model_runs`.

    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM ml.bus_matching_model_runs ORDER BY created_at;")
        return pd.DataFrame.from_records(
            cur.fetchall(), columns=[d.name for d in cur.description or []]
        )


def insert_trip_label(
    conn: psycopg.Connection,
    *,
    bus_id: str,
    date: datetime.date,
    trip_id: int,
    device_id: str | None,
    decision: Decision,
    mode: Mode,
    n_candidates: int,
) -> None:
    """Record one trip-level labeling decision.

    Args:
        conn: An open connection.
        bus_id: The bus.
        date: The trip date.
        trip_id: The specific trip this decision was made on.
        device_id: The chosen device, or `None` for "none of these"/unsure.
        decision: "match", "none_of_these", or "unsure".
        mode: Which UI mode produced this decision.
        n_candidates: How many candidates were shown.

    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ml.bus_matching_trip_labels
                (bus_id, date, trip_id, device_id, decision, mode, n_candidates)
            VALUES (%(bus_id)s, %(date)s, %(trip_id)s, %(device_id)s, %(decision)s,
                    %(mode)s, %(n_candidates)s)
            ON CONFLICT (bus_id, date, trip_id) DO UPDATE SET
                device_id = EXCLUDED.device_id,
                decision = EXCLUDED.decision,
                mode = EXCLUDED.mode,
                n_candidates = EXCLUDED.n_candidates,
                labeled_at = now();
            """,
            {
                "bus_id": bus_id,
                "date": date,
                "trip_id": trip_id,
                "device_id": device_id,
                "decision": decision,
                "mode": mode,
                "n_candidates": n_candidates,
            },
        )
