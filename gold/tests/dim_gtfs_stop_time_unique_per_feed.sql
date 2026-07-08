-- dbt singular test: passes if this returns zero rows.
-- Natural key is trip_id + stop_sequence, not trip_id + stop_id: a trip
-- can visit the same stop more than once (e.g. a loop route).
select feed_version_date, trip_id, stop_sequence, count(*)
from {{ ref('dim_gtfs_stop_time') }}
group by feed_version_date, trip_id, stop_sequence
having count(*) > 1
