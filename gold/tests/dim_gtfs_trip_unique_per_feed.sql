-- dbt singular test: passes if this returns zero rows.
select feed_version_date, trip_id, count(*)
from {{ ref('dim_gtfs_trip') }}
group by feed_version_date, trip_id
having count(*) > 1
