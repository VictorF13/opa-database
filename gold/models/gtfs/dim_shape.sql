-- One row per (feed_version_date, shape_id): the GTFS shape's individual
-- points aggregated into a single LineString, ordered by shape_pt_sequence.
-- Point-level granularity stays in silver.gtfs_shapes; this is the
-- dimensional/aggregated view gold is for.
select
    feed_version_date,
    shape_id,
    count(*) as point_count,
    st_makeline(geom order by shape_pt_sequence) as geom
from {{ source('silver', 'gtfs_shapes') }}
group by feed_version_date, shape_id
