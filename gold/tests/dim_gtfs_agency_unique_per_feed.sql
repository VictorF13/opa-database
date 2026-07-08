-- dbt singular test: passes if this returns zero rows.
select feed_version_date, agency_id, count(*)
from {{ ref('dim_gtfs_agency') }}
group by feed_version_date, agency_id
having count(*) > 1
