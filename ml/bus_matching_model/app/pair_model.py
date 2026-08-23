"""Train, apply, and drive labeling for the month-level pair model.

This is the layer that retires `belief.py`'s hand-set constants. Every
signal that used to arrive as a guessed weight -- how much a dictionary
hit is worth, how much cross-bus competition should suppress a
candidate, how much day-to-day continuity counts -- is a plain feature
in `pair_features.py`, and this module learns their weights from
`ml.bus_matching_pair_labels` instead.

The three things the pipeline still decides structurally, stated
plainly because they are *not* learned:

1. Which features exist (`pair_features.py`).
2. One device per bus (enforced by assignment, a physical constraint).
3. The ship threshold, which is chosen from *measured* precision on
   held-out labels (`precision_at_threshold`) rather than picked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import training
from metrics import evaluate as evaluate_metrics
from pair_features import pair_feature_names

if TYPE_CHECKING:
    import psycopg

# Below this many labeled pairs a fitted model is not worth trusting
# over the raw day-score ordering -- with a handful of labels the
# test-split metrics are themselves too noisy to tell a good model from
# a lucky one. Not a trust *weight* (there are none any more), just a
# floor on when to start using the model at all.
MIN_PAIRS_TO_TRAIN = 20
MIN_CLASS_COUNT = 5
BOTH_CLASSES = 2
TEST_FRAC = 0.3
RANDOM_SEED = 42

# A bus whose *best* candidate scores below this has no real evidence
# for anything, as opposed to competing evidence. Kept deliberately low
# -- it asks whether there is a signal at all, not whether the signal is
# good enough to ship -- so it only ever catches the genuinely empty
# cases.
MIN_EVIDENCE_SCORE = 0.01

# Precision measured on a handful of labels is not a measurement. Below
# this many labeled pairs above the threshold, `stopping_signal` reports
# "too early" instead of quoting a number that would swing wildly with
# the next click.
MIN_MEASURED_FOR_PRECISION = 20


def fetch_pair_labels(conn: psycopg.Connection) -> pd.DataFrame:
    """Fetch every recorded pair verdict.

    Args:
        conn: An open connection.

    Returns:
        Columns `bus_id`, `device_id`, `verdict`, `was_top_candidate`,
        `model_confidence`, `n_candidates`, `label_source`.

    """
    return pd.read_sql(
        "SELECT bus_id, device_id, verdict, was_top_candidate, "
        "model_confidence, n_candidates, label_source "
        "FROM ml.bus_matching_pair_labels;",
        conn,
    )


def insert_pair_label(
    conn: psycopg.Connection,
    *,
    bus_id: str,
    device_id: str,
    verdict: str,
    was_top_candidate: bool,
    model_confidence: float | None,
    n_candidates: int,
    label_source: str = "queue",
) -> None:
    """Record (or overwrite) one pair verdict.

    Args:
        conn: An open connection.
        bus_id: The bus.
        device_id: The device being judged for that bus.
        verdict: `"correct"`, `"wrong"`, or `"unsure"`.
        was_top_candidate: Whether this was the model's own top pick at
            labeling time -- lets evaluation separate "confirmed the
            model" from "overrode the model" after the fact.
        model_confidence: The model's P(correct) at labeling time, or
            `None` before any model exists.
        n_candidates: How many candidates were on screen.
        label_source: `"queue"` (ambiguity-ranked, biased toward hard
            cases) or `"audit"` (random confident sample, the only
            unbiased precision source). Never pool the two.

    """
    conn.execute(
        """
        INSERT INTO ml.bus_matching_pair_labels
            (bus_id, device_id, verdict, was_top_candidate,
             model_confidence, n_candidates, label_source)
        VALUES (%(bus_id)s, %(device_id)s, %(verdict)s, %(was_top)s,
                %(conf)s, %(n_candidates)s, %(source)s)
        ON CONFLICT (bus_id, device_id) DO UPDATE SET
            verdict = EXCLUDED.verdict,
            was_top_candidate = EXCLUDED.was_top_candidate,
            model_confidence = EXCLUDED.model_confidence,
            n_candidates = EXCLUDED.n_candidates,
            label_source = EXCLUDED.label_source,
            labeled_at = now();
        """,
        {
            "bus_id": bus_id,
            "device_id": device_id,
            "verdict": verdict,
            "was_top": was_top_candidate,
            "conf": model_confidence,
            "n_candidates": n_candidates,
            "source": label_source,
        },
    )


def build_training_rows(pairs: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    """Turn pair verdicts into binary training rows.

    A `"correct"` verdict is a positive for that pair *and* a negative
    for every other candidate of the same bus -- one device per bus, so
    confirming one rules out the rest. A `"wrong"` verdict is a single
    negative and says nothing about the others. `"unsure"` contributes
    nothing at all, which is the point of having it.

    Args:
        pairs: `pair_features.build_pair_features` output.
        labels: `fetch_pair_labels` output.

    Returns:
        `pairs` columns plus a boolean `label`, only for rows that a
        verdict actually implies.

    """
    if labels.empty:
        return pairs.iloc[0:0].assign(label=pd.Series(dtype=bool))

    decided = labels[labels["verdict"].isin(["correct", "wrong"])]
    if decided.empty:
        return pairs.iloc[0:0].assign(label=pd.Series(dtype=bool))

    positives = decided[decided["verdict"] == "correct"][["bus_id", "device_id"]]
    negatives = decided[decided["verdict"] == "wrong"][["bus_id", "device_id"]]

    # Every other candidate of a bus with a confirmed device is a
    # negative, which is what makes a single click worth many rows.
    implied = pairs.merge(positives[["bus_id"]], on="bus_id", how="inner")
    implied = implied.merge(
        positives.assign(_is_pos=True), on=["bus_id", "device_id"], how="left"
    )
    implied["label"] = implied["_is_pos"].fillna(value=False).astype(bool)
    implied = implied.drop(columns="_is_pos")

    explicit_neg = pairs.merge(negatives, on=["bus_id", "device_id"], how="inner")
    explicit_neg = explicit_neg.assign(label=False)

    rows = pd.concat([implied, explicit_neg], ignore_index=True)
    return rows.drop_duplicates(subset=["bus_id", "device_id"], keep="first")


def train_pair_model(rows: pd.DataFrame) -> dict[str, Any] | None:
    """Fit the pair model, held out by bus so no bus spans the split.

    Args:
        rows: `build_training_rows` output.

    Returns:
        `{"model", "selected_features", "metrics", "n_train", "n_test"}`,
        or `None` when there isn't yet enough labeled data to fit
        something worth trusting.

    """
    if rows.empty or len(rows) < MIN_PAIRS_TO_TRAIN:
        return None
    if rows["label"].sum() < MIN_CLASS_COUNT:
        return None
    if (~rows["label"]).sum() < MIN_CLASS_COUNT:
        return None

    names = [
        c for c in pair_feature_names(rows) if c != "label" and rows[c].dtype != object
    ]

    # Split by bus, not by row: a bus's positive and its negatives share
    # almost all their context, so letting them straddle the split would
    # leak and inflate every metric below.
    buses = rows["bus_id"].unique()
    rng = np.random.default_rng(RANDOM_SEED)
    shuffled = rng.permutation(buses)
    n_test = max(1, int(len(shuffled) * TEST_FRAC))
    test_buses = set(shuffled[:n_test])

    is_test = rows["bus_id"].isin(test_buses)
    train_df, test_df = rows[~is_test], rows[is_test]
    if train_df["label"].nunique() < BOTH_CLASSES:
        return None

    model = training.train_model(train_df[names], train_df["label"].to_numpy())

    metrics = {"auc": float("nan"), "brier": float("nan"), "ece": float("nan")}
    if not test_df.empty and test_df["label"].nunique() >= BOTH_CLASSES:
        preds = training.predict_positive_proba(model, test_df[names])
        metrics = evaluate_metrics(test_df["label"].to_numpy(), preds)

    return {
        "model": model,
        "selected_features": names,
        "metrics": metrics,
        "n_train": len(train_df),
        "n_test": len(test_df),
    }


def score_pairs(pairs: pd.DataFrame, state: dict[str, Any] | None) -> pd.Series:
    """Score P(correct) for every pair.

    Args:
        pairs: `pair_features.build_pair_features` output.
        state: `train_pair_model` output, or `None`.

    Returns:
        A float Series aligned to `pairs`. Before a model exists this
        falls back to the day model's own aggregated score, so the UI
        and the ordering work from the very first label rather than
        needing a bootstrap phase.

    """
    if state is None:
        return pairs["day_score_mean"].astype(float)
    preds = training.predict_positive_proba(
        state["model"], pairs[state["selected_features"]]
    )
    return pd.Series(preds, index=pairs.index)


def rank_pairs(pairs: pd.DataFrame, state: dict[str, Any] | None) -> pd.DataFrame:
    """Attach `pair_score` and per-bus ranking/margin to every pair.

    Args:
        pairs: `pair_features.build_pair_features` output.
        state: `train_pair_model` output, or `None`.

    Returns:
        `pairs` plus `pair_score`, `pair_rank` (1 = this bus's best),
        and `pair_margin` (top score minus runner-up, repeated on every
        row of the bus). `pair_margin` is the ambiguity measure the
        labeling queue sorts on.

    """
    out = pairs.copy()
    out["pair_score"] = score_pairs(out, state)
    by_bus = out.groupby("bus_id")["pair_score"]
    out["pair_rank"] = by_bus.rank(ascending=False, method="first")

    ordered = out.sort_values(["bus_id", "pair_score"], ascending=[True, False])
    top2 = ordered.groupby("bus_id")["pair_score"].nth(1).rename("runner_up")
    best = ordered.groupby("bus_id")["pair_score"].max().rename("best")
    margins = pd.concat([best, top2], axis=1)
    margins["pair_margin"] = margins["best"] - margins["runner_up"].fillna(0.0)

    return out.merge(
        margins[["pair_margin"]], left_on="bus_id", right_index=True, how="left"
    )


def select_hard_buses(
    ranked: pd.DataFrame, labels: pd.DataFrame, limit: int = 50
) -> pd.DataFrame:
    """Pick the buses whose answer is least settled -- the labeling queue.

    Ambiguity, not low confidence, is what makes a bus worth a human
    look: a bus whose top two candidates are nearly tied is one where a
    single decision resolves real uncertainty *and* generates several
    implied negatives. Buses already labeled (any verdict, including
    "unsure") drop out.

    **Buses with no real evidence for any candidate are excluded**, not
    ranked first. A bus whose best candidate scores ~1e-6 has a tiny
    top-to-runner-up margin purely because every option is near zero --
    that is absence of evidence, not ambiguity, and sorting on raw
    margin puts exactly those buses at the front of the queue. Confirmed
    live: without this filter the 10 "hardest" buses were all
    zero-evidence 67-prefix ones (best of 73 candidates scoring 5.7e-7),
    which would have wasted the entire first labeling session on buses
    whose true device has no AVL at all. They surface through
    `no_evidence_buses` instead, where they belong.

    Args:
        ranked: `rank_pairs` output.
        labels: `fetch_pair_labels` output.
        limit: How many buses to return.

    Returns:
        One row per unlabeled, evidence-bearing bus -- its top
        candidate, that candidate's score, and the bus's `pair_margin`
        -- ascending by margin, so the most genuinely confusing bus
        comes first.

    """
    tops = ranked[ranked["pair_rank"] == 1].copy()
    if not labels.empty:
        tops = tops[~tops["bus_id"].isin(set(labels["bus_id"]))]
    tops = tops[tops["pair_score"] >= MIN_EVIDENCE_SCORE]
    cols = ["bus_id", "device_id", "pair_score", "pair_margin", "n_candidates_for_bus"]
    return tops.sort_values("pair_margin")[cols].head(limit).reset_index(drop=True)


def no_evidence_buses(ranked: pd.DataFrame) -> pd.DataFrame:
    """Buses where not even the best candidate clears `MIN_EVIDENCE_SCORE`.

    Kept as an explicit, countable bucket rather than being silently
    mixed into the labeling queue -- these need candidate generation or
    a data source to change, not a human decision.

    Args:
        ranked: `rank_pairs` output.

    Returns:
        One row per such bus, descending by its (still tiny) best score.

    """
    tops = ranked[ranked["pair_rank"] == 1]
    out = tops[tops["pair_score"] < MIN_EVIDENCE_SCORE]
    cols = ["bus_id", "device_id", "pair_score", "n_candidates_for_bus"]
    return out.sort_values("pair_score", ascending=False)[cols].reset_index(drop=True)


def select_audit_buses(
    ranked: pd.DataFrame,
    labels: pd.DataFrame,
    threshold: float,
    limit: int = 50,
    seed: int = RANDOM_SEED,
) -> pd.DataFrame:
    """Randomly sample buses the model is already confident about.

    Plan Section 7's "random confident pairs -- small but
    non-negotiable": the ambiguity-ranked queue only ever shows hard
    cases, so labeling it can never reveal a *silent* error, a bus the
    model is confidently wrong about. Only a random sample of
    already-confident predictions can, and it is the sole source of an
    unbiased precision estimate.

    Random, not lowest-confidence-above-threshold: sampling the weakest
    of the confident ones would be biased in the other direction and
    would overstate the error rate.

    Args:
        ranked: `rank_pairs` output.
        labels: `fetch_pair_labels` output.
        threshold: Confidence cut defining "already confident".
        limit: How many buses to sample.
        seed: Sampling seed, so the audit set is reproducible.

    Returns:
        Same columns as `select_hard_buses`, in random order.

    """
    tops = ranked[(ranked["pair_rank"] == 1) & (ranked["pair_score"] >= threshold)]
    if not labels.empty:
        tops = tops[~tops["bus_id"].isin(set(labels["bus_id"]))]
    if tops.empty:
        return tops.head(0)
    cols = ["bus_id", "device_id", "pair_score", "pair_margin", "n_candidates_for_bus"]
    n = min(limit, len(tops))
    return tops.sample(n=n, random_state=seed)[cols].reset_index(drop=True)


def audit_precision(labels: pd.DataFrame) -> dict[str, Any]:
    """Unbiased precision, measured on audit-sampled labels only.

    Queue-sourced labels are deliberately drawn from the hardest cases,
    so pooling them with audit labels produces a number that means
    neither one thing nor the other. This function uses audit rows
    alone.

    Args:
        labels: `fetch_pair_labels` output (needs `label_source`).

    Returns:
        `{"n", "n_correct", "precision"}` over audit rows with a
        decisive verdict. `precision` is `NaN` until any exist.

    """
    if labels.empty or "label_source" not in labels.columns:
        return {"n": 0, "n_correct": 0, "precision": float("nan")}
    audit = labels[
        (labels["label_source"] == "audit")
        & (labels["verdict"].isin(["correct", "wrong"]))
    ]
    n = len(audit)
    n_correct = int((audit["verdict"] == "correct").sum()) if n else 0
    return {
        "n": n,
        "n_correct": n_correct,
        "precision": (n_correct / n) if n else float("nan"),
    }


def precision_at_threshold(
    ranked: pd.DataFrame, labels: pd.DataFrame, threshold: float
) -> dict[str, Any]:
    """Measure precision of "ship the top candidate above `threshold`".

    This is what turns the ship threshold from a guess into a choice:
    it reports, over the labeled pairs only, how often the model's own
    top pick above a given confidence was actually judged correct.

    Args:
        ranked: `rank_pairs` output.
        labels: `fetch_pair_labels` output.
        threshold: The confidence cut to evaluate.

    Returns:
        `{"n", "n_correct", "precision", "coverage_buses"}`. `precision`
        is `NaN` when no labeled pair clears the threshold yet.

    """
    tops = ranked[ranked["pair_rank"] == 1]
    judged = tops.merge(
        labels[labels["verdict"].isin(["correct", "wrong"])],
        on=["bus_id", "device_id"],
        how="inner",
    )
    above = judged[judged["pair_score"] >= threshold]
    n = len(above)
    n_correct = int((above["verdict"] == "correct").sum()) if n else 0
    return {
        "n": n,
        "n_correct": n_correct,
        "precision": (n_correct / n) if n else float("nan"),
        "coverage_buses": int((tops["pair_score"] >= threshold).sum()),
    }


def stopping_signal(
    ranked: pd.DataFrame,
    labels: pd.DataFrame,
    threshold: float,
    target_precision: float = 0.95,
) -> dict[str, Any]:
    """Report whether labeling is done, and if not, what is still missing.

    Deliberately descriptive rather than prescriptive: it reports the
    two numbers that actually decide the question -- how many buses are
    still unsettled, and what precision has been *measured* at the
    current threshold -- and only calls "done" for the unambiguous case.
    There is no invented convergence heuristic here; `target_precision`
    is a choice the caller makes with the measured number in front of
    them, not a fact about the data.

    Args:
        ranked: `rank_pairs` output.
        labels: `fetch_pair_labels` output.
        threshold: Current ship threshold.
        target_precision: The precision the caller wants before
            trusting the unlabeled remainder.

    Returns:
        `verdict` (`"too_early"`, `"keep_going"`, `"precision_low"`,
        `"queue_empty"`, `"done"`), a human-readable `message`, plus the
        underlying `remaining`, `precision`, and `n_measured` so the
        caller can show the evidence rather than just the conclusion.

    """
    prog = progress_summary(ranked, labels, threshold)
    # Audit labels only: queue labels are drawn from the hardest cases
    # *and* (in practice) tend to be confirmations of the top pick, so a
    # precision computed over them says nothing about whether confident
    # predictions are silently wrong -- which is the actual question
    # "can I stop?" depends on.
    prec = audit_precision(labels)
    queue = select_hard_buses(ranked, labels, limit=1)

    remaining = prog["remaining"]
    measured = prec["precision"]
    n_measured = prec["n"]
    base = {
        "remaining": remaining,
        "precision": measured,
        "n_measured": n_measured,
    }

    if n_measured < MIN_MEASURED_FOR_PRECISION:
        return {
            **base,
            "verdict": "too_early",
            "message": (
                f"Only {n_measured} audit-sampled labels so far -- not enough to "
                "measure precision. Switch to 'Audit confident picks' and label "
                f"~{MIN_MEASURED_FOR_PRECISION} of them: queue labels alone can "
                "never reveal a confidently-wrong prediction."
            ),
        }
    if measured < target_precision:
        return {
            **base,
            "verdict": "precision_low",
            "message": (
                f"Measured precision {measured:.1%} is below the "
                f"{target_precision:.0%} you asked for. Either keep labeling, "
                "or raise the threshold "
                "(fewer buses auto-accepted, each one safer)."
            ),
        }
    if queue.empty:
        return {
            **base,
            "verdict": "queue_empty",
            "message": (
                f"Nothing ambiguous left to label, and precision is {measured:.1%}. "
                f"{remaining} buses remain below the threshold -- they need "
                "candidate generation or new data, not more labeling."
            ),
        }
    if remaining == 0:
        return {
            **base,
            "verdict": "done",
            "message": (
                f"Every bus is settled and measured precision is {measured:.1%}. "
                "Safe to stop."
            ),
        }
    return {
        **base,
        "verdict": "keep_going",
        "message": (
            f"Precision {measured:.1%} at {threshold:.2f}, {remaining} buses still "
            "unsettled. Each label also implies negatives for that bus's rivals, "
            "so this number falls faster than one-per-click."
        ),
    }


def progress_summary(
    ranked: pd.DataFrame, labels: pd.DataFrame, threshold: float
) -> dict[str, Any]:
    """Summarize how much is settled, and how -- the UI's headline counters.

    Args:
        ranked: `rank_pairs` output.
        labels: `fetch_pair_labels` output.
        threshold: Current ship threshold.

    Returns:
        Counts of total buses, buses confirmed by hand, buses the model
        is confident about on its own, the union of those two (the real
        "done" number), and how many remain.

    """
    total_buses = int(ranked["bus_id"].nunique())
    confirmed = (
        set(labels[labels["verdict"] == "correct"]["bus_id"])
        if not labels.empty
        else set()
    )
    tops = ranked[ranked["pair_rank"] == 1]
    model_sure = set(tops[tops["pair_score"] >= threshold]["bus_id"])
    done = confirmed | model_sure
    return {
        "total_buses": total_buses,
        "confirmed_by_hand": len(confirmed),
        "model_confident": len(model_sure),
        "done": len(done),
        "remaining": total_buses - len(done),
        "labeled_any": int(labels["bus_id"].nunique()) if not labels.empty else 0,
    }
