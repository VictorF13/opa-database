-- dbt singular test: passes if this returns zero rows.
select fr.feed_version_date, fr.fare_id, fr.route_id
from {{ ref('dim_gtfs_fare_rule') }} as fr
left join {{ ref('dim_gtfs_fare') }} as f
    on f.feed_version_date = fr.feed_version_date and f.fare_id = fr.fare_id
where fr.fare_id is not null and f.fare_id is null
