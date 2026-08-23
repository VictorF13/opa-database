"""Buses that no AVL-based matcher can ever resolve, excluded everywhere.

Single source of truth, imported by the feature build, the pair layer,
and the assignment notebooks, so the three can never disagree about
which buses are in scope.

Two independent rules, because neither alone is sufficient:

1. **Company has no AVL at all.** Any `silver.dictionary_device`
   company where 100% of its known devices never ping in November 2023.
   Computed live rather than hardcoded, so it stays correct if this is
   ever re-run on other data. Confirmed live: COOTRAPS (265 devices,
   0 pinging) and Fretcar (76 devices, 0 pinging) -- though Fretcar
   turns out to have no valid trips in the period at all, so only
   COOTRAPS actually removes anything.
2. **Bus-number prefix.** On request, every `67`-prefix bus is excluded
   regardless of what the dictionary says. Rule 1 is dictionary-driven
   and therefore blind to vehicles missing from the snapshot: it caught
   240 of the 249 COOTRAPS buses, and the 9 it missed went on to
   dominate the labeling queue as apparent "hardest cases" (best of ~73
   candidates scoring 5.7e-7) before this rule existed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

# On request: excluded outright, not merely deprioritized. These buses
# have real AFC trips but their devices never appear in the AVL feed,
# so every candidate blocking finds for them is spatial coincidence.
EXCLUDED_BUS_PREFIXES = ("67",)

_NO_AVL_COMPANY_BUSES_SQL = """
    WITH company_ping_rates AS (
        SELECT
            d.company,
            count(DISTINCT d.device_id) AS n_devices,
            count(DISTINCT d.device_id) FILTER (
                WHERE EXISTS (
                    SELECT 1 FROM silver.avl_pings_y2023m11 p
                    WHERE p.device_id = d.device_id
                )
            ) AS n_pinging
        FROM silver.dictionary_device d
        WHERE d.device_id IS NOT NULL
        GROUP BY d.company
    ),
    no_avl AS (
        SELECT company FROM company_ping_rates
        WHERE n_devices > 0 AND n_pinging = 0
    ),
    normalized AS (
        SELECT regexp_replace(vehicle_number, '[^0-9]', '', 'g') AS digits
        FROM silver.dictionary_device
        WHERE company IN (SELECT company FROM no_avl)
    )
    SELECT DISTINCT
        CASE WHEN length(digits) < 5 THEN lpad(digits, 5, '0') ELSE digits END AS bus_id
    FROM normalized
    WHERE digits <> '';
"""


def excluded_bus_ids(conn: psycopg.Connection) -> set[str]:
    """Every bus id excluded from matching, by either rule.

    Args:
        conn: An open connection.

    Returns:
        Bus ids to drop. Callers should exclude these from candidates,
        features, assignment, and the final table alike -- they belong
        in an explicit `no_avl_data` bucket, not in any model's input.

    """
    real_buses = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT bus_id FROM ml.trip_validity_final WHERE is_valid;"
        ).fetchall()
    }
    by_company = {r[0] for r in conn.execute(_NO_AVL_COMPANY_BUSES_SQL).fetchall()}
    by_prefix = {b for b in real_buses if str(b).startswith(EXCLUDED_BUS_PREFIXES)}
    # Intersected with buses that actually ran: the dictionary lists
    # plenty of vehicles with no valid trips in the period, and counting
    # those as "excluded" would overstate how much is being dropped.
    return (by_company | by_prefix) & real_buses


def is_excluded(bus_id: str, excluded: set[str]) -> bool:
    """Whether one bus is excluded, prefix rule included.

    Args:
        bus_id: The bus to test.
        excluded: `excluded_bus_ids` output.

    Returns:
        `True` when the bus should be treated as structurally
        unmatchable.

    """
    return bus_id in excluded or str(bus_id).startswith(EXCLUDED_BUS_PREFIXES)
