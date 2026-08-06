"""Build the final all-November-2023 trip resolution table.

Every November 2023 AFC trip, resolved to a single row, mirroring the shape
of scratch.trip_match_predictions (vehicle_number, line_number, predicted
direction, trust_tier, ...) plus whatever AVL-side identity was actually
matched. Writes a BRAND NEW table, scratch.november_2023_trip_resolution --
never touches trip_match_predictions, trip_finder_predictions, or any other
existing table, per the "don't destroy or change any existing table"
constraint that governs this whole project.

vehicle_number is always the trip's own AFC-recorded bus number (straight
from silver.afc_boardings, zero translation) -- that IS the actual bus
number the whole project is for, per direct user instruction. The
AVL-side vehicle_id (avl_vehicle_id) is kept as its own raw id, no
dictionary crosswalk: the vehicle dictionaries in silver were checked and
found unreliable (conflicting bus numbers for the same AVL id across
snapshots, no coverage improvement from combining all five), so this table
does not attempt to translate an AVL id into a second "bus number".

Resolution waterfall, one row per trip:
  1. trip_labeler confident (trust_tier in high_confidence_valid/invalid,
     on either its 'vehicle' or 'device' source row) -> use that
     resolution. This is the EXACT same "has_good" condition
     find_candidates.py used to decide which trips needed trip_finder at
     all, so this reproduces the trip_labeler/trip_finder split precisely
     (896,579 trip_labeler-confident / 44,347 handed to trip_finder,
     verified against the live data before writing this).
  2. Else, if scratch.trip_finder_predictions has a row -> use that.
  3. Else -> unresolved (batches not finished yet, or the trip's
     line_number has no shape in any GTFS feed at all, so it was never
     scoreable by either model).

Run with: uv run tools/trip_finder/build_final_table.py
"""

from __future__ import annotations

import psycopg

DSN = "postgresql://opa:opa@localhost:5432/opa"

BUILD_SQL = """
DROP TABLE IF EXISTS scratch.november_2023_trip_resolution;

CREATE TABLE scratch.november_2023_trip_resolution AS
WITH trips AS (
    SELECT DISTINCT vehicle_number, line_number, trip_opened_at, trip_closed_at
    FROM silver.afc_boardings
    WHERE trip_opened_at >= '2023-11-01' AND trip_opened_at < '2023-12-01'
),
tl_vehicle AS (
    SELECT DISTINCT ON (vehicle_number, line_number, trip_opened_at, trip_closed_at)
        vehicle_number, line_number, trip_opened_at, trip_closed_at,
        entity_id::integer AS avl_vehicle_id,
        predicted_valid, valid_probability, predicted_shape_id,
        trust_tier, model_trained_n, predicted_at
    FROM scratch.trip_match_predictions
    WHERE source = 'vehicle'
      AND trust_tier IN ('high_confidence_valid', 'high_confidence_invalid')
    ORDER BY vehicle_number, line_number, trip_opened_at, trip_closed_at,
             predicted_at DESC
),
tl_device AS (
    SELECT DISTINCT ON (vehicle_number, line_number, trip_opened_at, trip_closed_at)
        vehicle_number, line_number, trip_opened_at, trip_closed_at,
        entity_id AS avl_device_id,
        predicted_valid, valid_probability, predicted_shape_id,
        trust_tier, model_trained_n, predicted_at
    FROM scratch.trip_match_predictions
    WHERE source = 'device'
      AND trust_tier IN ('high_confidence_valid', 'high_confidence_invalid')
    ORDER BY vehicle_number, line_number, trip_opened_at, trip_closed_at,
             predicted_at DESC
),
trip_labeler_resolved AS (
    SELECT
        t.vehicle_number, t.line_number, t.trip_opened_at, t.trip_closed_at,
        'trip_labeler'::text AS resolution_source,
        v.avl_vehicle_id,
        CASE WHEN v.avl_vehicle_id IS NULL THEN d.avl_device_id END AS avl_device_id,
        COALESCE(v.predicted_valid, d.predicted_valid) AS predicted_valid,
        COALESCE(v.valid_probability, d.valid_probability) AS valid_probability,
        CASE
            WHEN COALESCE(v.predicted_valid, d.predicted_valid) IS NOT TRUE
                THEN NULL
            WHEN right(COALESCE(v.predicted_shape_id, d.predicted_shape_id), 1) = 'I'
                THEN 'IDA'
            WHEN right(COALESCE(v.predicted_shape_id, d.predicted_shape_id), 1) = 'V'
                THEN 'VOLTA'
        END AS predicted_direction,
        NULL::text AS predicted_completeness,
        COALESCE(v.trust_tier, d.trust_tier) AS trust_tier,
        COALESCE(v.model_trained_n, d.model_trained_n) AS model_trained_n,
        COALESCE(v.predicted_at, d.predicted_at) AS predicted_at
    FROM trips t
    LEFT JOIN tl_vehicle v
      ON v.vehicle_number = t.vehicle_number AND v.line_number = t.line_number
     AND v.trip_opened_at = t.trip_opened_at AND v.trip_closed_at = t.trip_closed_at
    LEFT JOIN tl_device d
      ON d.vehicle_number = t.vehicle_number AND d.line_number = t.line_number
     AND d.trip_opened_at = t.trip_opened_at AND d.trip_closed_at = t.trip_closed_at
    WHERE v.avl_vehicle_id IS NOT NULL OR d.avl_device_id IS NOT NULL
),
trip_finder_resolved AS (
    SELECT
        f.vehicle_number, f.line_number, f.trip_opened_at, f.trip_closed_at,
        'trip_finder'::text AS resolution_source,
        f.predicted_candidate_vehicle_id AS avl_vehicle_id,
        NULL::text AS avl_device_id,
        f.predicted_valid,
        f.valid_probability,
        CASE
            WHEN f.predicted_completeness IS NULL THEN NULL
            WHEN left(f.predicted_completeness, 3) = 'IDA' THEN 'IDA'
            WHEN left(f.predicted_completeness, 5) = 'VOLTA' THEN 'VOLTA'
        END AS predicted_direction,
        f.predicted_completeness,
        f.trust_tier,
        f.model_trained_n,
        f.predicted_at
    FROM scratch.trip_finder_predictions f
    WHERE NOT EXISTS (
        SELECT 1 FROM trip_labeler_resolved tl
        WHERE tl.vehicle_number = f.vehicle_number
          AND tl.line_number = f.line_number
          AND tl.trip_opened_at = f.trip_opened_at
          AND tl.trip_closed_at = f.trip_closed_at
    )
),
resolved AS (
    SELECT * FROM trip_labeler_resolved
    UNION ALL
    SELECT * FROM trip_finder_resolved
)
SELECT
    t.vehicle_number, t.line_number, t.trip_opened_at, t.trip_closed_at,
    COALESCE(r.resolution_source, 'unresolved') AS resolution_source,
    r.avl_vehicle_id,
    r.avl_device_id,
    r.predicted_valid,
    r.valid_probability,
    r.predicted_direction,
    r.predicted_completeness,
    r.trust_tier,
    r.model_trained_n,
    r.predicted_at
FROM trips t
LEFT JOIN resolved r
  ON r.vehicle_number = t.vehicle_number AND r.line_number = t.line_number
 AND r.trip_opened_at = t.trip_opened_at AND r.trip_closed_at = t.trip_closed_at;

ALTER TABLE scratch.november_2023_trip_resolution
    ADD PRIMARY KEY (vehicle_number, line_number, trip_opened_at, trip_closed_at);

CREATE INDEX november_2023_trip_resolution_source_idx
    ON scratch.november_2023_trip_resolution (resolution_source);

CREATE INDEX november_2023_trip_resolution_trust_tier_idx
    ON scratch.november_2023_trip_resolution (trust_tier);
"""


def main() -> None:
    """Build scratch.november_2023_trip_resolution and print a coverage summary."""
    conn = psycopg.connect(DSN, autocommit=True)
    print("building scratch.november_2023_trip_resolution")
    conn.execute(BUILD_SQL)

    print("\nresolution_source breakdown:")
    for source, n in conn.execute(
        "SELECT resolution_source, count(*) FROM scratch.november_2023_trip_resolution "
        "GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {source}: {n}")

    print("\ntrust_tier breakdown:")
    for tier, n in conn.execute(
        "SELECT trust_tier, count(*) FROM scratch.november_2023_trip_resolution "
        "GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {tier}: {n}")

    row = conn.execute(
        "SELECT count(*) FROM scratch.november_2023_trip_resolution"
    ).fetchone()
    if row is None:
        msg = "count query returned no row"
        raise RuntimeError(msg)
    print(f"\ntotal rows: {row[0]}")
    conn.close()


if __name__ == "__main__":
    main()
