-- dbt singular test: passes if this returns zero rows.
select t.feed_version_date, t.trip_id, t.shape_id
from {{ ref('dim_gtfs_trip') }} as t
left join {{ ref('dim_shape') }} as s
    on s.feed_version_date = t.feed_version_date and s.shape_id = t.shape_id
where t.shape_id is not null and s.shape_id is null
