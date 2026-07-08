-- dbt singular test: passes if this returns zero rows.
-- Self-referential: parent_station is empty in the current data, but
-- this stays correct if a future feed ever populates it.
select child.feed_version_date, child.stop_id, child.parent_station
from {{ ref('dim_gtfs_stop') }} as child
left join {{ ref('dim_gtfs_stop') }} as parent
    on parent.feed_version_date = child.feed_version_date and parent.stop_id = child.parent_station
where child.parent_station is not null and parent.stop_id is null
