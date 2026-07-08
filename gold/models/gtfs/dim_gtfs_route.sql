-- One row per route, per feed export. References agency_id (both keyed
-- by the same feed_version_date).
select
    feed_version_date,
    route_id,
    agency_id,
    route_short_name,
    route_long_name,
    route_desc,
    route_type,
    route_url,
    route_color,
    route_text_color
from {{ source('silver', 'gtfs_routes') }}
