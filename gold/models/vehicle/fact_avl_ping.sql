{{
    config(
        indexes=[
            {'columns': ['master_vehicle_id']},
            {'columns': ['metric_timestamp']},
            {'columns': ['geom'], 'type': 'gist'},
        ]
    )
}}

-- All of silver.avl_pings, keyed by master_vehicle_id instead of the raw
-- vehicle_id: resolving this ping's vehicle to the conformed identity in
-- dim_vehicle_master unifies it with AFC's vehicle_number where a
-- confirmed match exists. Always populated: every AVL vehicle_id has a
-- master_vehicle_id, matched or not.
--
-- The raw vehicle_id is deliberately not carried here: it is fully
-- recoverable via dim_vehicle_master.gps_vehicle_id for any
-- master_vehicle_id (a 1:1 relationship by construction), so keeping both
-- would just be a redundant, source-specific id sitting next to the
-- conformed one gold is for. Same reasoning fact_afc_boarding already
-- follows: it never carried vehicle_number directly either, since that
-- lives on dim_afc_trip.
--
-- Indexes declared here (built after the table populates, not
-- incrementally maintained during the bulk insert) rather than any
-- drop-then-rebuild dance: dbt's postgres adapter already creates
-- indexes only after a table materialization finishes.
select
    dvm.master_vehicle_id,
    a.device_id,
    a.direction,
    a.odometer,
    a.route_code,
    a.speed,
    a.latitude,
    a.longitude,
    a.metric_timestamp,
    a.geom
from {{ source('silver', 'avl_pings') }} as a
left join {{ ref('dim_vehicle_master') }} as dvm
    on dvm.gps_vehicle_id = a.vehicle_id
