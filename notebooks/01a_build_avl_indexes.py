"""Stage -1: supporting indexes on silver.avl_pings for the scoring pipeline.

One-time DDL. avl_pings has no index on device_id or vehicle_id by default
(only metric_timestamp and geom, per partition), which forces a full-partition
seq scan for every per-vehicle time-window lookup that 01c/01d do. Once these
exist, rerunning this script is a no-op (CREATE INDEX IF NOT EXISTS).

Learned the hard way: the stock maintenance_work_mem (64MB) makes the index
build itself painfully slow on a table this size (~1.6B rows/year) -- bumping
it to 8GB (session-local, no config/container changes) took the first attempt
from "13+ minutes with ~0% visible progress" to "31 minutes total for both
indexes across all 12 partitions."

Run with: uv run notebooks/01a_build_avl_indexes.py
"""

from __future__ import annotations

import time

from _scoring_lib import open_connection


def main() -> None:
    """Build (device_id, metric_timestamp) and (vehicle_id, metric_timestamp)."""
    conn = open_connection()
    with conn.cursor() as cur:
        cur.execute("SET maintenance_work_mem = '8GB'")
        cur.execute("SET max_parallel_maintenance_workers = 6")

    # index names match what was actually built by hand during development
    # (avl_pings_device_ts_idx / avl_pings_vehicle_ts_idx, not
    # avl_pings_device_id_ts_idx) -- CREATE INDEX IF NOT EXISTS only
    # recognizes an index as already there if the name matches exactly.
    for col, short in (("device_id", "device"), ("vehicle_id", "vehicle")):
        index_name = f"avl_pings_{short}_ts_idx"
        t0 = time.time()
        print(f"{index_name}: building (or confirming already built) ...")
        with conn.cursor() as cur:
            # index_name/col come from the hardcoded tuple above, never user
            # input, so this f-string is not an injection vector.
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {index_name} "
                f"ON silver.avl_pings ({col}, metric_timestamp)"
            )
        conn.commit()
        print(f"  done ({time.time() - t0:.1f}s)")

    conn.close()


if __name__ == "__main__":
    main()
