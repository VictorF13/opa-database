-- dbt singular test: passes if this returns zero rows.
--
-- Joins on copied_from_feed_version_date when set, not feed_version_date
-- directly: a row whose whole stop_times table was substituted from
-- another export (see docs/architecture.md) carries that donor export's
-- stop_id namespace, not its own nominal feed_version_date's -- checking
-- against its own feed_version_date would flag every substituted row as
-- a false-positive broken reference.
select st.feed_version_date, st.trip_id, st.stop_sequence, st.stop_id
from {{ ref('dim_gtfs_stop_time') }} as st
left join {{ ref('dim_gtfs_stop') }} as s
    on s.feed_version_date = coalesce(st.copied_from_feed_version_date, st.feed_version_date)
    and s.stop_id = st.stop_id
where st.stop_id is not null and s.stop_id is null
