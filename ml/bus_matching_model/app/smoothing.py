"""Per-device temporal smoothing (plan Section 10).

Collapses each device's daily sequence of Section 9 (`assignment.py`)
assignments into `(bus_id, device_id, from_date, to_date)` intervals.

**Scope, confirmed live against the real assignment table**: of 1,437
devices with at least one assigned day, 1,407 (97.9%) stick to exactly
one bus for the entire month -- nothing to smooth, one trivial
interval. Only 30 devices (60 device-bus pairs) ever show more than one
bus, and 25 of those 60 pairs are a *single* day -- inspecting several
by hand showed the textbook pattern the plan describes: a solid run of
bus A, one day of bus B, the same solid run of A resuming. At this
scale a transparent, inspectable rule-based smoother is a better fit
than the plan's suggested HMM/changepoint-detection machinery, not an
under-implementation of it -- 30 devices can be read by eye if needed,
a fitted model would be opaque for no benefit here.

**Two rules, both gated by a cross-device conflict check**:

1. **Gap bridging** (plan Section 10.2): if a device shows the same bus
   on both sides of a run of unassigned calendar days, those days are
   folded into one interval -- *unless* any of those gap days has that
   bus **directly confirmed to a different device** by Section 9. A
   real, resolved conflict is not "missing data" (the plan's own rule:
   missing data is never negative evidence -- but this isn't missing,
   it's positive evidence for someone else), so those days break the
   run instead of being bridged. Confirmed live this matters: an early
   version without this check bridged a device with only 2 solved days
   14 days apart, straight through 6+ days where the same bus was
   solidly confirmed to a *different* device -- a real double-claim,
   not a smoothing nicety. `ml.bus_matching_intervals`'s own
   overlap-freeness (checked in the notebook) is the regression test
   for this.
2. **Isolated one-day deviation** (plan Section 10.3): a single day
   whose bus differs from both its immediate preceding *and* following
   assigned-day, when those two neighbors agree with each other, is
   corrected to that surrounding bus and logged -- gated by the same
   conflict check (the surrounding bus must not already be confirmed
   to some other device that exact day). A 2+-day run is never touched
   -- only exact single-day blips. A single day at the very start or
   end of a device's whole sequence (no both-sides neighbor to compare)
   is never corrected either.

Deliberately **not** implemented here (out of scope for what the real
data needed): a rolling-window mode filter or HMM for longer/noisier
sequences, since none exist in this month's data to justify it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Mapping

ONE_DAY = datetime.timedelta(days=1)

# A `bus_owner` mapping is `(bus_id, date) -> device_id` for the device
# Section 9 directly confirmed that day, for every day with a non-"none"
# assignment. A day absent from the map (bus didn't run, had no
# candidates, or resolved to "none") is never treated as a conflict --
# only a *different*, positively-confirmed device is.


@dataclass(frozen=True)
class DeviceRun:
    """One contiguous (post-smoothing) run of a device on one bus.

    Attributes:
        bus_id: The bus this run is assigned to.
        from_date: First assigned day in the run.
        to_date: Last assigned day in the run.
        n_days: Count of actually-assigned days within the run (may be
            less than the calendar span `to_date - from_date + 1` --
            the difference is bridged dead-AVL/no-candidate days).
        n_corrected: How many of `n_days` were isolated-deviation
            corrections rather than the solver's own direct output.

    """

    bus_id: str
    from_date: datetime.date
    to_date: datetime.date
    n_days: int
    n_corrected: int


def _gap_conflicts(
    bus_id: str,
    start: datetime.date,
    end: datetime.date,
    device_id: str,
    bus_owner: Mapping[tuple[str, datetime.date], str],
) -> bool:
    """Check for `bus_id` confirmed to another device on any day in `(start, end)`."""
    day = start + ONE_DAY
    while day < end:
        owner = bus_owner.get((bus_id, day))
        if owner is not None and owner != device_id:
            return True
        day += ONE_DAY
    return False


def _device_gap_conflicts(
    device_id: str,
    start: datetime.date,
    end: datetime.date,
    bus_id: str,
    device_owner: Mapping[tuple[str, datetime.date], str],
) -> bool:
    """Check for `device_id` confirmed to another bus on any day in `[start, end]`.

    Inclusive on both ends (unlike `_gap_conflicts`'s exclusive range) --
    harmless redundancy, since `start`/`end` are already-verified
    running days by the time a caller reaches this check, and it avoids
    an off-by-one trap on single-day gaps.
    """
    day = start
    while day <= end:
        owner = device_owner.get((device_id, day))
        if owner is not None and owner != bus_id:
            return True
        day += ONE_DAY
    return False


def _raw_runs(
    dates: list[datetime.date], buses: list[str]
) -> list[tuple[str, list[datetime.date]]]:
    """Group into runs of calendar-*and*-bus-consecutive days only -- no bridging."""
    runs: list[tuple[str, list[datetime.date]]] = []
    for date, bus_id in zip(dates, buses, strict=True):
        same_bus_adjacent = (
            runs and runs[-1][0] == bus_id and date - runs[-1][1][-1] == ONE_DAY
        )
        if same_bus_adjacent:
            runs[-1][1].append(date)
        else:
            runs.append((bus_id, [date]))
    return runs


def smooth_device_sequence(
    device_id: str,
    dates: list[datetime.date],
    buses: list[str],
    bus_owner: Mapping[tuple[str, datetime.date], str],
) -> tuple[list[DeviceRun], list[dict]]:
    """Smooth and collapse one device's assigned-day sequence into runs.

    Args:
        device_id: The device these observations belong to (needed to
            tell "this bus is confirmed to *me*" apart from "to someone
            else" when consulting `bus_owner`).
        dates: Assigned days for this device, ascending, no duplicates.
        buses: Same length as `dates` -- the bus assigned each day.
        bus_owner: See module docstring -- `(bus_id, date) -> device_id`.

    Returns:
        `(runs, corrections)`. `runs` are the final `DeviceRun`s.
        `corrections` is one dict per corrected day (`date`, `from_bus`,
        `to_bus`) for logging -- "a cluster of them on one device
        indicates a data problem worth investigating" (plan Section
        10.3).

    """
    raw_runs = _raw_runs(dates, buses)

    # Deviation detection happens on the *raw* (pre-bridge) runs, on
    # request-shaped intuition: "a single day sandwiched in an
    # otherwise-stable stretch" is about the observed sequence, not
    # about whichever gaps happen to bridge successfully. Computed in
    # one pass over the original data (not iteratively re-checked after
    # each correction) -- correcting one length-1 run can only ever
    # grow its neighbors, never create a *new* length-1 run elsewhere,
    # so a fixed point is reached in this single pass.
    corrections: list[dict] = []
    corrected_buses = list(buses)
    date_pos = {d: i for i, d in enumerate(dates)}
    for i, (bus_id, run_dates) in enumerate(raw_runs):
        is_interior = 0 < i < len(raw_runs) - 1
        if not is_interior or len(run_dates) != 1:
            continue
        surrounding_bus = raw_runs[i - 1][0]
        if surrounding_bus != raw_runs[i + 1][0] or surrounding_bus == bus_id:
            continue
        deviation_date = run_dates[0]
        owner = bus_owner.get((surrounding_bus, deviation_date))
        if owner not in (None, device_id):
            continue
        corrections.append(
            {"date": deviation_date, "from_bus": bus_id, "to_bus": surrounding_bus}
        )
        corrected_buses[date_pos[deviation_date]] = surrounding_bus

    # Re-run from scratch on the corrected labels, then bridge gaps --
    # reusing the same validated merge logic for both the "already
    # calendar-adjacent" and "bridged across a gap" cases, rather than
    # a second hand-rolled merge path that could (and did, before this
    # rewrite) skip the conflict check.
    corrected_runs = _raw_runs(dates, corrected_buses)
    merged: list[tuple[str, list[datetime.date]]] = []
    for bus_id, run_dates in corrected_runs:
        prev_bus, prev_dates = merged[-1] if merged else (None, None)
        can_merge = (
            merged
            and prev_bus == bus_id
            and not _gap_conflicts(
                bus_id, prev_dates[-1], run_dates[0], device_id, bus_owner
            )
        )
        if can_merge:
            merged[-1][1].extend(run_dates)
        else:
            merged.append((bus_id, list(run_dates)))

    corrected_dates = {c["date"] for c in corrections}
    device_runs = [
        DeviceRun(
            bus_id=bus_id,
            from_date=min(run_dates),
            to_date=max(run_dates),
            n_days=len(run_dates),
            n_corrected=sum(1 for d in run_dates if d in corrected_dates),
        )
        for bus_id, run_dates in merged
    ]
    return device_runs, corrections


def smooth_all_devices(assignments: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run `smooth_device_sequence` over every device in `assignments`.

    Args:
        assignments: Columns `bus_id`, `date`, `device_id` -- rows where
            a device was actually assigned to a bus (e.g.
            `ml.bus_matching_global_assignment` filtered to
            `device_id IS NOT NULL`). One row per `(bus_id, date)`, but
            grouped here by `device_id`.

    Returns:
        `(intervals, corrections)`. `intervals` has columns `device_id`,
        `bus_id`, `from_date`, `to_date`, `n_days`, `n_corrected`.
        `corrections` has columns `device_id`, `date`, `from_bus`,
        `to_bus`.

    """
    owner_cols = zip(
        assignments["bus_id"],
        assignments["date"],
        assignments["device_id"],
        strict=True,
    )
    bus_owner: dict[tuple[str, datetime.date], str] = {
        (bus_id, date): device_id for bus_id, date, device_id in owner_cols
    }

    interval_rows = []
    correction_rows = []
    for device_id, group in assignments.sort_values("date").groupby("device_id"):
        runs, corrections = smooth_device_sequence(
            device_id, group["date"].tolist(), group["bus_id"].tolist(), bus_owner
        )
        interval_rows.extend(
            {
                "device_id": device_id,
                "bus_id": run.bus_id,
                "from_date": run.from_date,
                "to_date": run.to_date,
                "n_days": run.n_days,
                "n_corrected": run.n_corrected,
            }
            for run in runs
        )
        correction_rows.extend({"device_id": device_id, **c} for c in corrections)

    intervals = pd.DataFrame(
        interval_rows,
        columns=[
            "device_id",
            "bus_id",
            "from_date",
            "to_date",
            "n_days",
            "n_corrected",
        ],
    )
    corrections_df = pd.DataFrame(
        correction_rows, columns=["device_id", "date", "from_bus", "to_bus"]
    )
    return intervals, corrections_df


def build_device_owner(
    assignments: pd.DataFrame, corrections: pd.DataFrame
) -> dict[tuple[str, datetime.date], str]:
    """Build `extend_to_bus_bounds`'s `device_owner` from smoothed, not raw, data.

    Confirmed live this distinction matters: building it straight from
    `assignments` (Section 9's raw per-day output) and using that to
    gate `extend_to_bus_bounds` split intervals on phantom conflicts --
    days `smooth_all_devices` had *already* corrected away as isolated
    one-day noise (recorded in `corrections`) still showed their old,
    overridden bus in the raw table, so extension treated Section 10's
    own resolved noise as if it were a live, unresolved conflict.
    Applying `corrections` on top before use fixes it.

    Args:
        assignments: Same frame passed to `smooth_all_devices`.
        corrections: That call's second return value.

    Returns:
        `(device_id, date) -> bus_id`, corrections applied.

    """
    device_owner = {
        (device_id, date): bus_id
        for bus_id, date, device_id in zip(
            assignments["bus_id"],
            assignments["date"],
            assignments["device_id"],
            strict=True,
        )
    }
    for device_id, date, to_bus in zip(
        corrections["device_id"],
        corrections["date"],
        corrections["to_bus"],
        strict=True,
    ):
        device_owner[device_id, date] = to_bus
    return device_owner


def _extend_one_bus(
    bus_id: str,
    device_id: str,
    group: pd.DataFrame,
    running_dates: list[datetime.date],
    device_owner: Mapping[tuple[str, datetime.date], str],
) -> list[dict]:
    """`extend_to_bus_bounds` for a single already-single-device bus."""
    # Calendar days already inside one of this bus's own Section 10
    # interval spans -- includes that section's *own* bridged days, not
    # just its directly-solved ones (those stay distinguishable via each
    # original row's own n_days/n_corrected, preserved below; this set
    # is only for "already accounted for" vs "new").
    already_covered: set[datetime.date] = set()
    for row in group.itertuples(index=False):
        day = row.from_date
        while day <= row.to_date:
            already_covered.add(day)
            day += ONE_DAY

    # Walk the bus's own *running* days in order (never a calendar day it
    # didn't operate -- that's not a gap to bridge, just a day off).
    # Skip a day this bus's device is directly confirmed elsewhere on;
    # for every other day, merge it into the current run only if the
    # *entire calendar range* back to the run's last day (not just
    # running days) is conflict-free -- checking only individual running
    # days would silently bridge straight through a conflict that falls
    # entirely on days this bus simply has no trips on. Confirmed live
    # this matters: a device legitimately confirmed to a different bus
    # for a few days this bus never runs on got bridged through when
    # this check only looked at running days one at a time instead of
    # the full range between them.
    runs: list[list[datetime.date]] = []
    for day in running_dates:
        is_available = day in already_covered or device_owner.get((device_id, day)) in (
            None,
            bus_id,
        )
        if not is_available:
            continue
        if runs and not _device_gap_conflicts(
            device_id, runs[-1][-1], day, bus_id, device_owner
        ):
            runs[-1].append(day)
        else:
            runs.append([day])

    rows = []
    for run_dates in runs:
        from_date, to_date = min(run_dates), max(run_dates)
        subsumed = group[
            (group["from_date"] >= from_date) & (group["to_date"] <= to_date)
        ]
        n_already_covered = sum(1 for d in run_dates if d in already_covered)
        rows.append(
            {
                "device_id": device_id,
                "bus_id": bus_id,
                "from_date": from_date,
                "to_date": to_date,
                "n_days": int(subsumed["n_days"].sum()),
                "n_corrected": int(subsumed["n_corrected"].sum()),
                "n_extended": len(run_dates) - n_already_covered,
            }
        )
    return rows


def extend_to_bus_bounds(
    intervals: pd.DataFrame,
    bus_running_dates: Mapping[str, list[datetime.date]],
    device_owner: Mapping[tuple[str, datetime.date], str],
) -> pd.DataFrame:
    """Extend single-device buses' intervals to their full running range.

    On request, from a domain expert: a device essentially never changes
    bus mid-month ("hardly ever, perhaps once every blue moon"), so once
    a bus has exactly one distinct confirmed device across *all* its
    Section 10 intervals, that device very likely covers every day the
    bus ran that month -- not just the days Section 9 happened to find
    enough direct evidence for. Extends before the earliest interval,
    after the latest, and through any gap between same-device intervals.

    Still gated by the same cross-device conflict check as gap-bridging
    (never extend into a day where the target device is already
    confirmed to a *different* bus) -- "hardly ever" is not "never", and
    a bus's own running-date range can include a day genuinely covered
    by the rare real swap.

    Buses with more than one distinct device across their intervals are
    left completely untouched -- that's exactly the rare swap case this
    rule doesn't try to resolve, on request (ambiguous which device to
    extrapolate into the gaps for those).

    Args:
        intervals: `smooth_all_devices` output.
        bus_running_dates: `bus_id -> every date that bus had a valid
            trip` (not just days it has an interval for).
        device_owner: `(device_id, date) -> bus_id` for every day
            Section 9 directly confirmed that device to some bus --
            built from the same source as `smooth_all_devices`'s
            internal `bus_owner`, just keyed the other way around.

    Returns:
        A new intervals-shaped `DataFrame` (same columns as
        `smooth_all_devices`, plus `n_extended`: days added beyond what
        was directly solved). Untouched (multi-device) buses' original
        rows pass through with `n_extended=0`.

    """
    rows = []
    for bus_id, group in intervals.groupby("bus_id"):
        device_ids = group["device_id"].unique()
        running_dates = sorted(bus_running_dates.get(bus_id, []))
        if len(device_ids) != 1 or not running_dates:
            rows.extend({**r, "n_extended": 0} for r in group.to_dict("records"))
            continue
        rows.extend(
            _extend_one_bus(bus_id, device_ids[0], group, running_dates, device_owner)
        )

    return pd.DataFrame(
        rows,
        columns=[
            "device_id",
            "bus_id",
            "from_date",
            "to_date",
            "n_days",
            "n_corrected",
            "n_extended",
        ],
    )
