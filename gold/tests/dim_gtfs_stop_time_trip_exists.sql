-- dbt singular test: passes if this returns zero rows.
--
-- Joins on copied_from_feed_version_date when set, not feed_version_date
-- directly: a row whose whole stop_times table was substituted from
-- another export (see docs/architecture.md) carries that donor export's
-- trip_id namespace, not its own nominal feed_version_date's -- checking
-- against its own feed_version_date would flag every substituted row as
-- a false-positive broken reference.
select st.feed_version_date, st.trip_id, st.stop_sequence
from {{ ref('dim_gtfs_stop_time') }} as st
left join {{ ref('dim_gtfs_trip') }} as t
    on t.feed_version_date = coalesce(st.copied_from_feed_version_date, st.feed_version_date)
    and t.trip_id = st.trip_id
where st.trip_id is not null and t.trip_id is null
