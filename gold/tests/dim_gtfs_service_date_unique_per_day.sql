-- dbt singular test: passes if this returns zero rows.
-- Avoids a dbt_utils dependency for one composite-uniqueness check.
select feed_version_date, service_id, calendar_date, count(*)
from {{ ref('dim_gtfs_service_date') }}
group by feed_version_date, service_id, calendar_date
having count(*) > 1
