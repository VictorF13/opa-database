-- One row per scheduled trip, per feed export. References route_id,
-- service_id (see dim_gtfs_service_date for actual operating dates),
-- and shape_id (see dim_shape), all within the same feed_version_date.
-- Not to be confused with dim_afc_trip, which is a real observed vehicle
-- run from the fare-collection system, not a GTFS schedule definition.
select
    feed_version_date,
    route_id,
    service_id,
    trip_id,
    trip_headsign,
    trip_short_name,
    direction_id,
    block_id,
    shape_id,
    wheelchair_accessible
from {{ source('silver', 'gtfs_trips') }}
