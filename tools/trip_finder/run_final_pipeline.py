"""Wait for find_candidates.py to finish, then run the full final-output pipeline.

find_candidates.py (launched separately, long-running) generates one
BATCH_SIZE-trip parquet file at a time under data/batches/ until every trip
in its ~44,347-trip target population has been scored, then exits on its
own (prints "done"). This script polls for that process to actually exit
-- not just for a batch-file count, which could race a partially-written
last batch -- and only then runs the two already-verified final steps in
order:

  1. predict_trips.py's main() -- scores every candidate now sitting in
     data/batches/*.parquet and (re)writes scratch.trip_finder_predictions.
  2. build_final_table.py's main() -- rebuilds
     scratch.november_2023_trip_resolution from trip_labeler's existing
     predictions plus the now-complete trip_finder predictions.

Both write only to their own brand-new tables (never
scratch.trip_finder_labels or scratch.trip_match_predictions), same as
running them by hand. Meant to be launched detached (nohup) right alongside
find_candidates.py so the final tables get rebuilt automatically the
moment all batches land, with no one needing to babysit it.

Run with:
  nohup uv run tools/trip_finder/run_final_pipeline.py \
      > run_final_pipeline.log 2>&1 &
"""

from __future__ import annotations

import subprocess
import time

import build_final_table
import predict_trips

POLL_SECONDS = 60
FIND_CANDIDATES_PATTERN = "tools/trip_finder/find_candidates.py"


def find_candidates_running() -> bool:
    """Check whether find_candidates.py is still an active process."""
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/pgrep", "-f", FIND_CANDIDATES_PATTERN],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def main() -> None:
    """Poll until find_candidates.py exits, then run prediction + final table build."""
    if not find_candidates_running():
        print(
            "find_candidates.py is not currently running -- assuming batches are "
            "already final and proceeding immediately.",
            flush=True,
        )
    else:
        print(
            "waiting for find_candidates.py to finish generating batches...", flush=True
        )
        while find_candidates_running():
            time.sleep(POLL_SECONDS)
        print("find_candidates.py has exited.", flush=True)

    print("\n=== running predict_trips.py ===", flush=True)
    predict_trips.main()

    print("\n=== running build_final_table.py ===", flush=True)
    build_final_table.main()

    print("\npipeline complete.", flush=True)


if __name__ == "__main__":
    main()
