-- One row per stop/station, per feed export. parent_station
-- self-references stop_id (within the same feed_version_date) for stops
-- that belong to a larger station; nullable, and empty in the current
-- data. geom is carried through as-is from silver, already generated
-- there from stop_lat/stop_lon.
select
    feed_version_date,
    stop_id,
    stop_code,
    stop_name,
    stop_desc,
    stop_lat,
    stop_lon,
    zone_id,
    stop_url,
    location_type,
    parent_station,
    stop_timezone,
    wheelchair_boarding,
    geom
from {{ source('silver', 'gtfs_stops') }}
