"""Stage 3a: score every dictionary_device (vehicle_number -> device_id) pair.

Requires 01b_build_reference_data.py (and 01a_build_avl_indexes.py, for
reasonable runtime) to have already run. Processes one month at a time,
committing after each -- if this run is killed or cancelled partway (as
happened twice during development, once from a RAM/disk scare, once to apply
a query fix), rerunning this script picks up from the next unfinished month
instead of starting over.

Run with: uv run notebooks/01c_score_device_mapping.py
"""

from __future__ import annotations

from _scoring_lib import (
    OUTPUT_DIR,
    open_connection,
    score_candidate_mapping,
    write_parquet,
)

_CANDIDATES_CTE = """
    SELECT vehicle_number, device_id
    FROM silver.dictionary_device
    WHERE snapshot_date = (
        SELECT MAX(snapshot_date) FROM silver.dictionary_device
    )
      AND device_id IS NOT NULL
    """


def spot_check(device_scores, vehicle_number: str, line_number: str) -> None:  # noqa: ANN001
    """Print the known-good case (bus 14922, device ep1-428115079, line 15).

    Cross-checked by hand earlier against GTFS-shape plots and trip-map plots
    in an exploratory notebook -- whichever shape scores the highest
    progress_corr here should match the direction visually confirmed there.
    """
    subset = device_scores.loc[
        (device_scores["vehicle_number"] == vehicle_number)
        & (device_scores["line_number"] == line_number)
    ].sort_values(["trip_opened_at", "shape_id"])
    header = f"vehicle {vehicle_number}, line {line_number}: {len(subset)} rows"
    print(f"\nspot check: {header}")
    cols = [
        "trip_opened_at",
        "trip_closed_at",
        "resolved_feed_version_date",
        "shape_id",
        "direction_id",
        "n_pings_in_window",
        "avg_dist_to_line_m",
        "progress_corr",
        "start_proximity_m",
        "end_proximity_m",
        "implied_speed_kmh",
        "speed_percentile",
    ]
    print(subset[cols].head(20).to_string())


def main() -> None:
    """Run Stage 3a end-to-end (resuming any partially-done months) and export."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = open_connection()

    device_scores_df = score_candidate_mapping(
        conn,
        "scratch.device_mapping_trip_scores",
        _CANDIDATES_CTE,
        "device_id",
        "device_id",
    )
    write_parquet(
        device_scores_df, OUTPUT_DIR / "device_mapping_trip_scores_2023.parquet"
    )
    spot_check(device_scores_df, vehicle_number="14922", line_number="15")

    conn.close()


if __name__ == "__main__":
    main()
