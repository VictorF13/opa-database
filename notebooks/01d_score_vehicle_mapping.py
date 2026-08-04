"""Stage 3b: score every dictionary_vehicle (cod_veiculo -> id_veiculo) pair.

Ambiguous cod_veiculo (multiple id_veiculo in the same snapshot) are kept
as-is, one candidate row each -- not filtered out.

Requires 01b_build_reference_data.py (and 01a_build_avl_indexes.py, for
reasonable runtime) to have already run. Processes one month at a time,
committing after each -- if this run is killed or cancelled partway, rerunning
this script picks up from the next unfinished month instead of starting over.

Run with: uv run notebooks/01d_score_vehicle_mapping.py
"""

from __future__ import annotations

from _scoring_lib import (
    OUTPUT_DIR,
    open_connection,
    score_candidate_mapping,
    write_parquet,
)

_CANDIDATES_CTE = """
    SELECT
        cod_veiculo AS vehicle_number,
        id_veiculo::integer AS vehicle_id
    FROM silver.dictionary_vehicle
    WHERE snapshot_date = (
        SELECT MAX(snapshot_date) FROM silver.dictionary_vehicle
    )
      AND id_veiculo IS NOT NULL
    """


def main() -> None:
    """Run Stage 3b end-to-end (resuming any partially-done months) and export."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = open_connection()

    vehicle_scores_df = score_candidate_mapping(
        conn,
        "scratch.vehicle_mapping_trip_scores",
        _CANDIDATES_CTE,
        "vehicle_id",
        "vehicle_id",
    )
    write_parquet(
        vehicle_scores_df, OUTPUT_DIR / "vehicle_mapping_trip_scores_2023.parquet"
    )

    conn.close()


if __name__ == "__main__":
    main()
