-- dbt singular test: passes if this returns zero rows.
select feed_version_date, stop_id, count(*)
from {{ ref('dim_gtfs_stop') }}
group by feed_version_date, stop_id
having count(*) > 1
