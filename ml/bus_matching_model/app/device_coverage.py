"""The other half of Section 13: which real devices never got claimed.

`ml.bus_matching_final_pairs` (`final_output.py`) is bus-centric -- for
every bus, which device, if any. That leaves an asymmetry unanswered: a
device can be genuinely active all month (tens of thousands of real
pings) and still never appear anywhere in that table, either because it
lost a competition for a bus another device won, or because blocking
never even considered it for anyone. Confirmed live: of 1,493 devices
that pinged at all in November 2023, 62 are claimed by no bus.

This module answers "why" for each of those 62, at the same level of
honesty as `final_output.py`: every device gets a `reason`, none are
silently unexplained.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    import psycopg

_ACTIVE_DEVICES_SQL = """
    SELECT
        device_id,
        count(*) AS n_pings,
        count(DISTINCT metric_timestamp::date) AS n_days_active,
        min(metric_timestamp::date) AS first_active_date,
        max(metric_timestamp::date) AS last_active_date
    FROM silver.avl_pings_y2023m11
    GROUP BY device_id;
"""

_CANDIDATE_BUSES_SQL = """
    SELECT DISTINCT device_id, bus_id FROM ml.bus_matching_candidates;
"""

_DICTIONARY_BUSES_SQL = """
    SELECT DISTINCT
        device_id,
        CASE WHEN length(regexp_replace(vehicle_number, '[^0-9]', '', 'g')) < 5
             THEN lpad(regexp_replace(vehicle_number, '[^0-9]', '', 'g'), 5, '0')
             ELSE regexp_replace(vehicle_number, '[^0-9]', '', 'g') END AS bus_id
    FROM silver.dictionary_device
    WHERE device_id IS NOT NULL
      AND regexp_replace(vehicle_number, '[^0-9]', '', 'g') <> '';
"""


def build_unclaimed_devices(
    conn: psycopg.Connection, ranked: pd.DataFrame, excluded: set[str]
) -> pd.DataFrame:
    """List every active device that no bus claims, and why.

    Args:
        conn: An open connection.
        ranked: `pair_model.rank_pairs` output -- used to report a
            device's best-scoring bus, when it had any in-scope
            candidacy at all.
        excluded: `exclusions.excluded_bus_ids` output.

    Returns:
        One row per unclaimed device: `device_id`, `n_pings`,
        `n_days_active`, `first_active_date`, `last_active_date`,
        `in_dictionary`, `dictionary_bus_ids`, `best_candidate_bus_id`,
        `best_candidate_score`, `reason`. `reason` is one of
        `never_blocked` (blocking found no bus for it at all),
        `blocked_only_to_excluded_bus` (its only candidacy was a
        67-prefix bus), or `lost_competition` (it competed for a
        real bus and another device won).

    """
    active = pd.read_sql(_ACTIVE_DEVICES_SQL, conn)
    claimed = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT device_id FROM ml.bus_matching_final_pairs "
            "WHERE device_id IS NOT NULL;"
        ).fetchall()
    }
    unclaimed = active[~active["device_id"].isin(claimed)].copy()
    if unclaimed.empty:
        return unclaimed

    candidates = pd.read_sql(_CANDIDATE_BUSES_SQL, conn)
    candidates_by_device = candidates.groupby("device_id")["bus_id"].apply(list)

    dictionary = pd.read_sql(_DICTIONARY_BUSES_SQL, conn)
    dict_by_device = dictionary.groupby("device_id")["bus_id"].apply(sorted)

    best = (
        ranked.sort_values("pair_score", ascending=False)
        .drop_duplicates(subset="device_id", keep="first")
        .set_index("device_id")[["bus_id", "pair_score"]]
        if not ranked.empty
        else pd.DataFrame(columns=["bus_id", "pair_score"])
    )

    def _classify(device_id: str) -> tuple[str, str | None, float | None]:
        buses = candidates_by_device.get(device_id, [])
        in_scope_buses = [b for b in buses if b not in excluded]
        if not buses:
            return "never_blocked", None, None
        if not in_scope_buses:
            return "blocked_only_to_excluded_bus", None, None
        if device_id in best.index:
            row = best.loc[device_id]
            return "lost_competition", row["bus_id"], float(row["pair_score"])
        return "lost_competition", None, None

    reasons = unclaimed["device_id"].apply(_classify)
    unclaimed["reason"] = reasons.apply(lambda t: t[0])
    unclaimed["best_candidate_bus_id"] = reasons.apply(lambda t: t[1])
    unclaimed["best_candidate_score"] = reasons.apply(lambda t: t[2])

    unclaimed["in_dictionary"] = unclaimed["device_id"].isin(dict_by_device.index)
    unclaimed["dictionary_bus_ids"] = unclaimed["device_id"].apply(
        lambda d: ",".join(dict_by_device.get(d, [])) or None
    )

    return unclaimed.sort_values("n_pings", ascending=False).reset_index(drop=True)


def save_unclaimed_devices(conn: psycopg.Connection, table: pd.DataFrame) -> None:
    """Replace the persisted unclaimed-devices table wholesale.

    Args:
        conn: An open connection.
        table: `build_unclaimed_devices` output.

    """
    with conn.transaction():
        conn.execute("TRUNCATE ml.bus_matching_unclaimed_devices;")
        with conn.cursor().copy(
            "COPY ml.bus_matching_unclaimed_devices "
            "(device_id, n_pings, n_days_active, first_active_date, "
            "last_active_date, in_dictionary, dictionary_bus_ids, "
            "best_candidate_bus_id, best_candidate_score, reason) FROM STDIN"
        ) as copy:
            for row in table.itertuples(index=False):
                copy.write_row(
                    (
                        row.device_id,
                        int(row.n_pings),
                        int(row.n_days_active),
                        row.first_active_date,
                        row.last_active_date,
                        bool(row.in_dictionary),
                        row.dictionary_bus_ids,
                        row.best_candidate_bus_id,
                        row.best_candidate_score,
                        row.reason,
                    )
                )
