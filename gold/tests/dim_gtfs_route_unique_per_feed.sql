-- dbt singular test: passes if this returns zero rows.
select feed_version_date, route_id, count(*)
from {{ ref('dim_gtfs_route') }}
group by feed_version_date, route_id
having count(*) > 1
