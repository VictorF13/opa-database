"""Per-date global assignment (plan Section 9).

`belief.compute_date_beliefs`'s cross-bus-date suppression is a single,
non-iterated pairwise-discount heuristic -- cheap enough for the live
labeling UI, but not an actual joint solve, so two buses can still end
up claiming the same device if neither individually clears the
suppression threshold. This module replaces that heuristic, per date,
with a real minimum-cost bipartite matching: every bus is matched to at
most one device (or explicitly "none"), and no device is matched to more
than one bus that day.

Consumes `belief.compute_raw_beliefs`'s *pre-suppression* posterior
(never the suppressed one -- see that function's docstring) as the cost
input, since this is what the suppression heuristic itself approximates.

Sized for real per-date graphs (confirmed live: ~1,600 buses x ~1,400
devices, ~16,500 candidate edges on a weekday -- about 0.7% dense), so
`scipy.sparse.csgraph.min_weight_full_bipartite_matching` (sparse LAPJVsp)
is used rather than a dense `scipy.optimize.linear_sum_assignment`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from belief import NONE_OPTION
from scipy.sparse import coo_array
from scipy.sparse.csgraph import min_weight_full_bipartite_matching

# posterior=0 would make -log(posterior) infinite and posterior=1 would
# make the "none" alternative for that bus look impossibly bad (an
# LAPJVsp edge weight of exactly 0 is also invalid -- "non-zero weights"
# per scipy's own docs) -- clip away from both ends.
POSTERIOR_FLOOR = 1e-9
POSTERIOR_CEIL = 1 - 1e-9


@dataclass(frozen=True)
class DateAssignment:
    """One date's global assignment result.

    Attributes:
        bus_id: `(n_buses,)`.
        device_id: `(n_buses,)`, `None` where the bus was assigned "none".
        cost: `(n_buses,)` the assigned edge's cost (`-log(posterior)`).
        margin: `(n_buses,)` cost increase if this bus's assigned edge
            were forbidden and the date re-solved -- large means this
            assignment was competitive across the whole date, not just
            locally; small means a near-tie (plan Section 9.3).

    """

    bus_id: np.ndarray
    device_id: np.ndarray
    cost: np.ndarray
    margin: np.ndarray


def _none_option_cost(raw_beliefs: pd.DataFrame) -> pd.Series:
    """Per-bus `-log(posterior)` for the `NONE_OPTION` row, indexed by `bus_id`."""
    none_rows = raw_beliefs[raw_beliefs["option"] == NONE_OPTION]
    posterior = none_rows["posterior"].clip(POSTERIOR_FLOOR, POSTERIOR_CEIL)
    return pd.Series(
        (-np.log(posterior)).to_numpy(), index=none_rows["bus_id"].to_numpy()
    )


def build_cost_matrix(
    raw_beliefs: pd.DataFrame,
) -> tuple[coo_array, np.ndarray, np.ndarray]:
    """Build one date's sparse bus-x-(device+dummy) cost matrix.

    Args:
        raw_beliefs: `belief.compute_raw_beliefs` output for a *single*
            date (one date's `candidates`/`votes` only -- this function
            doesn't group by date itself).

    Returns:
        `(matrix, bus_ids, device_ids)`. `matrix` is `(n_buses,
        n_devices + n_buses)`: columns `[0, n_devices)` are real devices
        in `device_ids` order, columns `[n_devices, n_devices +
        n_buses)` are each bus's own "assign to none" dummy (column
        `n_devices + i` belongs to `bus_ids[i]`, no cross edges, so a
        bus can never be forced onto another bus's dummy).

    """
    device_rows = raw_beliefs[raw_beliefs["option"] != NONE_OPTION]
    bus_ids = np.sort(raw_beliefs["bus_id"].unique())
    device_ids = np.sort(device_rows["option"].unique())
    bus_idx = pd.Series(np.arange(len(bus_ids)), index=bus_ids)
    device_idx = pd.Series(np.arange(len(device_ids)), index=device_ids)

    posterior = device_rows["posterior"].clip(POSTERIOR_FLOOR, POSTERIOR_CEIL)
    cost = -np.log(posterior).to_numpy()
    rows = bus_idx.loc[device_rows["bus_id"]].to_numpy()
    cols = device_idx.loc[device_rows["option"]].to_numpy()

    none_cost = _none_option_cost(raw_beliefs).loc[bus_ids].to_numpy()
    dummy_rows = np.arange(len(bus_ids))
    dummy_cols = len(device_ids) + np.arange(len(bus_ids))

    all_rows = np.concatenate([rows, dummy_rows])
    all_cols = np.concatenate([cols, dummy_cols])
    all_data = np.concatenate([cost, none_cost])
    shape = (len(bus_ids), len(device_ids) + len(bus_ids))
    matrix = coo_array((all_data, (all_rows, all_cols)), shape=shape)
    return matrix, bus_ids, device_ids


def _decode_assignment(
    row_ind: np.ndarray,
    col_ind: np.ndarray,
    cost_lookup: dict[tuple[int, int], float],
    device_ids: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    n_devices = len(device_ids)
    device_id = np.array(
        [device_ids[c] if c < n_devices else None for c in col_ind], dtype=object
    )
    cost = np.array([cost_lookup[r, c] for r, c in zip(row_ind, col_ind, strict=True)])
    return device_id, cost


def solve_date(
    raw_beliefs: pd.DataFrame, *, compute_margins: bool = True
) -> DateAssignment:
    """Solve one date's global assignment via minimum-weight full bipartite matching.

    Args:
        raw_beliefs: `belief.compute_raw_beliefs` output for a single date.
        compute_margins: If `True`, re-solve once per bus with that
            bus's assigned edge forbidden, to get a real cross-date
            competitiveness margin (plan Section 9.3) instead of a
            locally-cheap proxy. `O(n_buses)` re-solves; confirmed
            feasible at real per-date graph sizes -- see the module's
            benchmark notebook cell.

    Returns:
        `DateAssignment` for every bus with at least one candidate that
        date (buses with zero candidates aren't in `raw_beliefs` at
        all and must be handled by the caller as trivially unassigned).

    """
    matrix, bus_ids, device_ids = build_cost_matrix(raw_beliefs)
    coo = matrix.tocoo()
    cost_lookup = dict(zip(zip(coo.row, coo.col, strict=True), coo.data, strict=True))

    row_ind, col_ind = min_weight_full_bipartite_matching(matrix.tocsr())
    device_id, cost = _decode_assignment(row_ind, col_ind, cost_lookup, device_ids)
    orig_total = float(cost.sum())

    margin = np.full(len(bus_ids), np.nan)
    if compute_margins:
        shape = matrix.shape
        for i in range(len(bus_ids)):
            keep = ~((coo.row == row_ind[i]) & (coo.col == col_ind[i]))
            alt_matrix = coo_array(
                (coo.data[keep], (coo.row[keep], coo.col[keep])), shape=shape
            ).tocsr()
            alt_row, alt_col = min_weight_full_bipartite_matching(alt_matrix)
            alt_total = sum(
                cost_lookup[r, c] for r, c in zip(alt_row, alt_col, strict=True)
            )
            margin[i] = alt_total - orig_total

    return DateAssignment(bus_id=bus_ids, device_id=device_id, cost=cost, margin=margin)
