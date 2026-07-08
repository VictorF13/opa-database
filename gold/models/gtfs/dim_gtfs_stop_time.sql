{{ config(indexes=[{'columns': ['feed_version_date', 'trip_id']}]) }}

-- One row per (feed_version_date, trip_id, stop_sequence): the natural
-- key here is trip_id + stop_sequence, not trip_id + stop_id, since
-- stop_sequence is what disambiguates a trip visiting the same stop more
-- than once (e.g. a loop route). References trip_id and stop_id, both
-- within the same feed_version_date. Biggest of the GTFS gold tables
-- (a few million rows), hence the index for the join it will most
-- commonly be used in (by trip).
select
    feed_version_date,
    trip_id,
    arrival_time,
    departure_time,
    stop_id,
    stop_sequence,
    stop_headsign,
    pickup_type,
    drop_off_type,
    shape_dist_traveled
from {{ source('silver', 'gtfs_stop_times') }}
