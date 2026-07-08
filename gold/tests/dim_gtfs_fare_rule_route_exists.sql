-- dbt singular test: passes if this returns zero rows.
select fr.feed_version_date, fr.fare_id, fr.route_id
from {{ ref('dim_gtfs_fare_rule') }} as fr
left join {{ ref('dim_gtfs_route') }} as r
    on r.feed_version_date = fr.feed_version_date and r.route_id = fr.route_id
where fr.route_id is not null and r.route_id is null
