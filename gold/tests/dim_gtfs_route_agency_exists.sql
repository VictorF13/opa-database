-- dbt singular test: passes if this returns zero rows.
-- Every route's agency_id must exist in dim_gtfs_agency for the same
-- feed_version_date (a plain dbt `relationships` test would only check
-- agency_id in isolation, which is meaningless here since ids repeat
-- across different feed_version_date snapshots).
select r.feed_version_date, r.route_id, r.agency_id
from {{ ref('dim_gtfs_route') }} as r
left join {{ ref('dim_gtfs_agency') }} as a
    on a.feed_version_date = r.feed_version_date and a.agency_id = r.agency_id
where r.agency_id is not null and a.agency_id is null
