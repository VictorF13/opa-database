-- One row per fare/route/zone rule, per feed export. A bridge table by
-- design (a fare can apply to several routes, and vice versa), so there
-- is no single-column or simple composite natural key to test uniqueness
-- on here, unlike the other GTFS gold tables.
select
    feed_version_date,
    fare_id,
    route_id,
    origin_id,
    destination_id,
    contains_id
from {{ source('silver', 'gtfs_fare_rules') }}
