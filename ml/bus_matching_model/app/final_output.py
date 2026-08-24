"""Section 13's final output: one settled answer per bus for the month.

Every bus that ran gets exactly one row (a device-swap bus gets one row
per interval instead) with an explicit `method` saying how it was
settled -- following the same convention `ml.bus_matching_global_assignment`
already uses (`method IN ('global_assignment', 'no_candidates',
'no_avl_data')`), just at the month grain instead of the bus-date grain.
Nothing is silently dropped: a bus that cannot be resolved still gets a
row, with a method that says why.

**Split detection.** Not every `verdict='unsure'` label means the same
thing -- some are a real device swap mid-month (two devices, each
confidently correct on its own disjoint stretch of dates), some are the
user genuinely not knowing. `detect_split` tells them apart from the
day-score time series itself: a swap shows up as one confident device,
then a single clean changeover, then a different confident device,
never as several changeovers or two devices confident on the same day
without an obvious handover. Confirmed live on two buses caught by
hand (12225, 35403): both detect as clean single-changeover splits at
exactly the dates the day-score table showed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd
from pair_model import MIN_EVIDENCE_SCORE

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    import psycopg

# A day counts as "confident" for split detection at the same bar the
# rest of the pipeline calls a match trustworthy -- see
# pair_model.MIN_EVIDENCE_SCORE for the opposite (no-signal) bar.
SPLIT_CONFIDENCE = 0.9

# Below this many confident days, a device's "block" is too thin to
# trust as a real occupancy stretch rather than a one-off lucky score.
MIN_BLOCK_DAYS = 2

# More than this many distinct confident devices for one bus is not a
# swap, it is genuine multi-way ambiguity -- flagged for human review
# rather than guessed at.
MAX_SPLIT_DEVICES = 2


def detect_split(day_scores: pd.DataFrame) -> dict[str, Any]:
    """Classify one bus's day-score time series across all its candidates.

    Args:
        day_scores: Columns `date`, `device_id`, `day_score` -- every
            candidate device for one bus, every date the day-level
            feature build produced a row (present even on days with no
            AVL data, scored near zero).

    Returns:
        `{"kind": "single", "device_id", "n_confident_days"}` when only
        one device is ever confident (the "unsure" verdict undersold a
        bus that is not actually ambiguous);
        `{"kind": "split", "intervals": [...]}` when exactly two devices
        each hold a clean, contiguous confident block with one
        changeover between them -- each interval has `device_id`,
        `start_date`, `end_date`, `n_confident_days`, and `note` for the
        overlap/gap case;
        `{"kind": "unclear", "reason": str}` otherwise -- more than two
        confident devices, more than one changeover, or too few
        confident days to say anything -- meant for human review, not
        an auto-resolution.

    """
    confident = day_scores[day_scores["day_score"] >= SPLIT_CONFIDENCE].copy()
    if confident.empty:
        return {"kind": "unclear", "reason": "no confident days for any candidate"}

    # One winner per date: the rare overlap day (both devices confident,
    # the true handover point) is resolved to whichever scored higher.
    winners = (
        confident.sort_values("day_score", ascending=False)
        .drop_duplicates(subset="date", keep="first")
        .sort_values("date")
    )
    overlap_dates = confident["date"][confident["date"].duplicated(keep=False)].unique()

    devices = winners["device_id"].unique()
    if len(devices) == 1:
        return {
            "kind": "single",
            "device_id": devices[0],
            "n_confident_days": len(winners),
        }
    if len(devices) > MAX_SPLIT_DEVICES:
        return {
            "kind": "unclear",
            "reason": f"{len(devices)} distinct devices confident on different days",
        }

    # Exactly two devices: count changeovers in chronological order.
    # A real swap has exactly one; anything else is genuine flip-flop
    # ambiguity, not a clean handover.
    sequence = winners["device_id"].to_numpy()
    n_switches = int((sequence[1:] != sequence[:-1]).sum())
    if n_switches != 1:
        return {
            "kind": "unclear",
            "reason": f"{n_switches} changeovers between {len(devices)} devices "
            "(a clean swap has exactly one)",
        }

    intervals = []
    for device_id, block in winners.groupby("device_id", sort=False):
        if len(block) < MIN_BLOCK_DAYS:
            return {
                "kind": "unclear",
                "reason": f"device {device_id} only confident on {len(block)} "
                f"day(s), too thin to trust as a real block",
            }
        note = (
            "overlap day resolved to higher score"
            if any(d in block["date"].to_numpy() for d in overlap_dates)
            else ""
        )
        intervals.append(
            {
                "device_id": device_id,
                "start_date": block["date"].min(),
                "end_date": block["date"].max(),
                "n_confident_days": len(block),
                "note": note,
            }
        )
    intervals.sort(key=lambda i: i["start_date"])
    return {"kind": "split", "intervals": intervals}


def build_final_pairs(
    conn: psycopg.Connection,
    day: pd.DataFrame,
    ranked: pd.DataFrame,
    labels: pd.DataFrame,
    excluded: set[str],
    threshold: float,
) -> pd.DataFrame:
    """Build one settled row per bus (more for a detected split).

    Priority per bus, each bus landing in exactly one bucket:

    1. `excluded_no_avl` -- structurally unmatchable (`exclusions.py`).
    2. `no_candidates` -- blocking found nothing at all.
    3. `hand_confirmed` -- a `verdict='correct'` label exists.
    4. An `unsure` label exists -> `detect_split` decides between
       `split_detected`, `resolved_after_review` (turned out to have
       only one real confident device after all), or `needs_review`.
    5. `no_evidence` -- has candidates, but the best score never clears
       `pair_model.MIN_EVIDENCE_SCORE`.
    6. `pair_model` -- best candidate clears `threshold`.
    7. `below_threshold` -- has real evidence, just not enough of it yet.

    Args:
        conn: An open connection.
        day: Concatenated day features with a `day_score` column
            (`training.predict_positive_proba` output already attached).
        ranked: `pair_model.rank_pairs` output.
        labels: `pair_model.fetch_pair_labels` output.
        excluded: `exclusions.excluded_bus_ids` output.
        threshold: The ship threshold.

    Returns:
        One row per settled bus (or interval), columns `bus_id`,
        `device_id`, `start_date`, `end_date`, `confidence`, `method`,
        `n_days_with_data`, `notes`.

    """
    all_buses = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT bus_id FROM ml.trip_validity_final WHERE is_valid;"
        ).fetchall()
    }
    month_start = conn.execute(
        "SELECT min(trip_date) FROM ml.trip_validity_final WHERE is_valid;"
    ).fetchone()[0]
    month_end = conn.execute(
        "SELECT max(trip_date) FROM ml.trip_validity_final WHERE is_valid;"
    ).fetchone()[0]

    buses_with_candidates = set(ranked["bus_id"].unique())
    tops = ranked[ranked["pair_rank"] == 1].set_index("bus_id")
    correct_labels = (
        labels[labels["verdict"] == "correct"]
        .drop_duplicates(subset="bus_id", keep="last")
        .set_index("bus_id")
    )
    unsure_buses = set(labels[labels["verdict"] == "unsure"]["bus_id"])
    day_by_bus = dict(list(day.groupby("bus_id")))

    rows: list[dict[str, Any]] = []
    for bus_id in sorted(all_buses):
        rows.extend(
            _classify_bus(
                bus_id,
                excluded=excluded,
                buses_with_candidates=buses_with_candidates,
                tops=tops,
                correct_labels=correct_labels,
                unsure_buses=unsure_buses,
                day_by_bus=day_by_bus,
                threshold=threshold,
                month_start=month_start,
                month_end=month_end,
            )
        )

    return pd.DataFrame(rows)


def _base_row(
    bus_id: str, month_start: date, month_end: date, **kw: object
) -> dict[str, Any]:
    return {
        "bus_id": bus_id,
        "device_id": None,
        "start_date": month_start,
        "end_date": month_end,
        "confidence": None,
        "n_days_with_data": None,
        "notes": "",
        **kw,
    }


def _rows_from_split_result(
    result: dict[str, Any], base: Callable[..., dict[str, Any]]
) -> list[dict[str, Any]]:
    """Turn one `detect_split` verdict into final-table rows for that bus."""
    if result["kind"] == "split":
        return [
            base(
                device_id=interval["device_id"],
                start_date=interval["start_date"],
                end_date=interval["end_date"],
                confidence=SPLIT_CONFIDENCE,
                n_days_with_data=interval["n_confident_days"],
                method="split_detected",
                notes=interval["note"],
            )
            for interval in result["intervals"]
        ]
    if result["kind"] == "single":
        return [
            base(
                device_id=result["device_id"],
                confidence=SPLIT_CONFIDENCE,
                n_days_with_data=result["n_confident_days"],
                method="resolved_after_review",
                notes="unsure label, but only one device was ever "
                "confident -- not actually ambiguous",
            )
        ]
    return [base(method="needs_review", notes=result["reason"])]


def _classify_bus(
    bus_id: str,
    *,
    excluded: set[str],
    buses_with_candidates: set[str],
    tops: pd.DataFrame,
    correct_labels: pd.DataFrame,
    unsure_buses: set[str],
    day_by_bus: dict[str, pd.DataFrame],
    threshold: float,
    month_start: date,
    month_end: date,
) -> list[dict[str, Any]]:
    """Settle one bus into its bucket.

    See `build_final_pairs` for the priority order this follows.
    """

    def base(**kw: object) -> dict[str, Any]:
        return _base_row(bus_id, month_start, month_end, **kw)

    if bus_id in excluded:
        return [base(method="excluded_no_avl")]
    if bus_id not in buses_with_candidates:
        return [base(method="no_candidates")]

    if bus_id in correct_labels.index:
        lab = correct_labels.loc[bus_id]
        top = tops.loc[bus_id] if bus_id in tops.index else None
        return [
            base(
                device_id=lab["device_id"],
                confidence=float(lab["model_confidence"] or 1.0),
                n_days_with_data=(
                    int(top["n_days_with_data"]) if top is not None else None
                ),
                method="hand_confirmed",
            )
        ]

    if bus_id in unsure_buses:
        cols = ["date", "device_id", "day_score"]
        bus_scores = day_by_bus.get(bus_id)
        bus_day_scores = (
            bus_scores[cols] if bus_scores is not None else pd.DataFrame(columns=cols)
        )
        return _rows_from_split_result(detect_split(bus_day_scores), base)

    top = tops.loc[bus_id]
    score = float(top["pair_score"])
    if score < MIN_EVIDENCE_SCORE:
        return [base(method="no_evidence")]
    method = "pair_model" if score >= threshold else "below_threshold"
    return [
        base(
            device_id=top["device_id"],
            confidence=score,
            n_days_with_data=int(top["n_days_with_data"]),
            method=method,
        )
    ]


def save_final_pairs(conn: psycopg.Connection, table: pd.DataFrame) -> None:
    """Replace the persisted final table wholesale.

    Args:
        conn: An open connection.
        table: `build_final_pairs` output.

    """
    with conn.transaction():
        conn.execute("TRUNCATE ml.bus_matching_final_pairs;")
        with conn.cursor().copy(
            "COPY ml.bus_matching_final_pairs "
            "(bus_id, device_id, start_date, end_date, confidence, "
            "n_days_with_data, method, notes) FROM STDIN"
        ) as copy:
            for row in table.itertuples(index=False):
                n_days = row.n_days_with_data
                copy.write_row(
                    (
                        row.bus_id,
                        row.device_id,
                        row.start_date,
                        row.end_date,
                        row.confidence,
                        None if n_days is None or pd.isna(n_days) else int(n_days),
                        row.method,
                        row.notes,
                    )
                )
