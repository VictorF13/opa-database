-- dbt singular test: passes if this returns zero rows.
select feed_version_date, fare_id, count(*)
from {{ ref('dim_gtfs_fare') }}
group by feed_version_date, fare_id
having count(*) > 1
