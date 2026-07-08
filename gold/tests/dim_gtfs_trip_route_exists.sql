-- dbt singular test: passes if this returns zero rows.
select t.feed_version_date, t.trip_id, t.route_id
from {{ ref('dim_gtfs_trip') }} as t
left join {{ ref('dim_gtfs_route') }} as r
    on r.feed_version_date = t.feed_version_date and r.route_id = t.route_id
where t.route_id is not null and r.route_id is null
