"""Build an empirical bus-number -> AVL-identity dictionary for November 2023.

Every prior administrative vehicle dictionary in silver was checked and
found unreliable (see tools/trip_finder/build_final_table.py's docstring):
conflicting bus numbers for the same AVL id across snapshots, no coverage
gain from combining all five, none dated near November 2023. This script
builds a different kind of dictionary instead -- not from an administrative
roster, but empirically, from the actual accumulated evidence across all
three prediction sources built in this project:

  - scratch.trip_match_predictions, source='vehicle': trip_labeler testing
    a trip's own AFC-reported bus against its own route via AVL vehicle_id
    identity.
  - scratch.trip_match_predictions, source='device': the same, via AVL
    device_id identity.
  - scratch.trip_finder_predictions: trip_finder's open search across every
    AVL-tracked vehicle active in a trip's window, for trips trip_labeler
    wasn't confident about either way.

For each AFC vehicle_number (bus number), every high_confidence_valid trip
across those three sources is a (bus_number -> AVL identity) data point.
When a bus number's confident trips overwhelmingly agree on one identity,
that agreement itself is stronger evidence of the bus's real AVL identity
than any administrative snapshot -- this is the "starting perfect data"
the resulting table represents. A bus qualifies as STRONG on a given
identity type (device_id or avl_vehicle_id) when >= STRONG_MIN_AGREEMENT
of its confident trips for that type agree on the same value, with at
least STRONG_MIN_TRIPS supporting trips; the two identity types are kept
independent, since a bus can have a strong device_id, a strong
avl_vehicle_id, both, or neither.

A fourth source, scratch.vehicle_identity_confirmed, holds manual sign-offs
from tools/vehicle_identity_labeler's "Confirm this bus" button -- a human
looking at the actual GPS tracks decided a bus number's real identity
directly, for buses the automatic sources alone couldn't resolve. Those
always win over the automatic computation for the same bus number (a human
confirmation is a hard override, not one more statistical vote); read-only
here, same as trip_match_predictions/trip_finder_predictions.

avl_vehicle_id here is the raw AVL vehicle_id (silver.avl_pings.vehicle_id
space), not administrative cod_veiculo -- consistent with
build_final_table.py, no dictionary crosswalk.

validity_start/validity_end are fixed to the November 2023 window this
whole project's evidence comes from -- this dictionary is not claimed to
hold for any other month.

Writes a BRAND NEW table, scratch.november_2023_vehicle_identity. Never
touches trip_match_predictions, trip_finder_predictions,
vehicle_identity_confirmed, or any other existing table.

Run with: uv run tools/trip_finder/build_vehicle_identity.py
"""

from __future__ import annotations

import psycopg

DSN = "postgresql://opa:opa@localhost:5432/opa"
# Kept in sync by hand with the literal 0.9 / 3 thresholds in BUILD_SQL below --
# an f-string there would make BUILD_SQL a plain str instead of LiteralString,
# which psycopg's execute() overloads reject.
STRONG_MIN_AGREEMENT = 0.9
STRONG_MIN_TRIPS = 3

BUILD_SQL = """
CREATE TABLE IF NOT EXISTS scratch.vehicle_identity_confirmed (
    vehicle_number text PRIMARY KEY,
    avl_vehicle_id integer,
    device_id text,
    agreement_pct double precision,
    confidence_lower_bound double precision,
    total_trials integer,
    confirmed_at timestamptz NOT NULL
);

DROP TABLE IF EXISTS scratch.november_2023_vehicle_identity;

CREATE TABLE scratch.november_2023_vehicle_identity AS
WITH vehicle_evidence AS (
    SELECT vehicle_number, entity_id::integer AS avl_vehicle_id
    FROM scratch.trip_match_predictions
    WHERE source = 'vehicle' AND trust_tier = 'high_confidence_valid'
    UNION ALL
    SELECT vehicle_number, predicted_candidate_vehicle_id AS avl_vehicle_id
    FROM scratch.trip_finder_predictions
    WHERE trust_tier = 'high_confidence_valid'
      AND predicted_candidate_vehicle_id IS NOT NULL
),
device_evidence AS (
    SELECT vehicle_number, entity_id AS device_id
    FROM scratch.trip_match_predictions
    WHERE source = 'device' AND trust_tier = 'high_confidence_valid'
),
vehicle_grp AS (
    SELECT vehicle_number, avl_vehicle_id, count(*) AS n
    FROM vehicle_evidence GROUP BY 1, 2
),
device_grp AS (
    SELECT vehicle_number, device_id, count(*) AS n
    FROM device_evidence GROUP BY 1, 2
),
vehicle_best AS (
    SELECT DISTINCT ON (vehicle_number)
        vehicle_number, avl_vehicle_id, n AS top_n,
        sum(n) OVER (PARTITION BY vehicle_number) AS total_n
    FROM vehicle_grp
    ORDER BY vehicle_number, n DESC
),
device_best AS (
    SELECT DISTINCT ON (vehicle_number)
        vehicle_number, device_id, n AS top_n,
        sum(n) OVER (PARTITION BY vehicle_number) AS total_n
    FROM device_grp
    ORDER BY vehicle_number, n DESC
),
-- excludes manually-confirmed buses so the automatic computation never
-- fights with a human sign-off for the same bus number
vehicle_strong AS (
    SELECT vehicle_number, avl_vehicle_id,
           top_n AS avl_vehicle_id_support_trips,
           top_n::double precision / total_n AS avl_vehicle_id_agreement_pct
    FROM vehicle_best
    WHERE total_n >= 3
      AND top_n::double precision / total_n >= 0.9
      AND vehicle_number NOT IN (
          SELECT vehicle_number FROM scratch.vehicle_identity_confirmed
      )
),
device_strong AS (
    SELECT vehicle_number, device_id,
           top_n AS device_id_support_trips,
           top_n::double precision / total_n AS device_id_agreement_pct
    FROM device_best
    WHERE total_n >= 3
      AND top_n::double precision / total_n >= 0.9
      AND vehicle_number NOT IN (
          SELECT vehicle_number FROM scratch.vehicle_identity_confirmed
      )
),
automatic AS (
    SELECT
        COALESCE(v.vehicle_number, d.vehicle_number) AS vehicle_number,
        d.device_id, d.device_id_support_trips, d.device_id_agreement_pct,
        v.avl_vehicle_id, v.avl_vehicle_id_support_trips,
        v.avl_vehicle_id_agreement_pct
    FROM vehicle_strong v
    FULL JOIN device_strong d USING (vehicle_number)
),
-- manual confirmations always win over the automatic computation for the
-- same bus number; both id-type columns share the one combined judgment
-- since a confirmation isn't measured separately per identity type
confirmed AS (
    SELECT
        vehicle_number,
        device_id, total_trials AS device_id_support_trips,
        agreement_pct AS device_id_agreement_pct,
        avl_vehicle_id, total_trials AS avl_vehicle_id_support_trips,
        agreement_pct AS avl_vehicle_id_agreement_pct
    FROM scratch.vehicle_identity_confirmed
)
SELECT
    vehicle_number,
    DATE '2023-11-01' AS validity_start,
    DATE '2023-11-30' AS validity_end,
    device_id, device_id_support_trips, device_id_agreement_pct,
    avl_vehicle_id, avl_vehicle_id_support_trips, avl_vehicle_id_agreement_pct,
    now() AS computed_at
FROM automatic
UNION ALL
SELECT
    vehicle_number,
    DATE '2023-11-01', DATE '2023-11-30',
    device_id, device_id_support_trips, device_id_agreement_pct,
    avl_vehicle_id, avl_vehicle_id_support_trips, avl_vehicle_id_agreement_pct,
    now()
FROM confirmed;

ALTER TABLE scratch.november_2023_vehicle_identity ADD PRIMARY KEY (vehicle_number);
"""


def main() -> None:
    """Build scratch.november_2023_vehicle_identity and print a coverage summary."""
    conn = psycopg.connect(DSN, autocommit=True)
    print("building scratch.november_2023_vehicle_identity")
    conn.execute(BUILD_SQL)

    row = conn.execute(
        "SELECT count(*), "
        "count(*) FILTER (WHERE device_id IS NOT NULL), "
        "count(*) FILTER (WHERE avl_vehicle_id IS NOT NULL), "
        "count(*) FILTER (WHERE device_id IS NOT NULL AND avl_vehicle_id IS NOT NULL) "
        "FROM scratch.november_2023_vehicle_identity"
    ).fetchone()
    if row is None:
        msg = "summary query returned no row"
        raise RuntimeError(msg)
    total, with_device, with_vehicle, with_both = row
    print(f"\n{total} bus numbers with a strong identity")
    print(f"  strong device_id: {with_device}")
    print(f"  strong avl_vehicle_id: {with_vehicle}")
    print(f"  strong on both: {with_both}")
    conn.close()


if __name__ == "__main__":
    main()
