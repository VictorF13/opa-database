-- One row per (feed_version_date, service_id, calendar_date) that the
-- service actually operated on: gtfs_calendar's weekly pattern (day of
-- week flags over a date range), expanded into real calendar dates, then
-- corrected by gtfs_calendar_dates' exceptions (exception_type 1 adds a
-- date the weekly pattern wouldn't otherwise include, 2 removes one it
-- would). Precomputed once here so nothing downstream has to redo the
-- day-of-week and exception logic itself.
--
-- This describes one feed snapshot's own calendar; it says nothing about
-- which feed_version_date was valid on a given real date. Pair with
-- dim_gtfs_feed_version for that.
with weekly_pattern_expanded as (
    select
        c.feed_version_date,
        c.service_id,
        gs.calendar_date::date as calendar_date
    from {{ source('silver', 'gtfs_calendar') }} as c
    cross join lateral generate_series(c.start_date, c.end_date, interval '1 day') as gs (calendar_date)
    where
        (extract(dow from gs.calendar_date) = 0 and c.sunday = 1)
        or (extract(dow from gs.calendar_date) = 1 and c.monday = 1)
        or (extract(dow from gs.calendar_date) = 2 and c.tuesday = 1)
        or (extract(dow from gs.calendar_date) = 3 and c.wednesday = 1)
        or (extract(dow from gs.calendar_date) = 4 and c.thursday = 1)
        or (extract(dow from gs.calendar_date) = 5 and c.friday = 1)
        or (extract(dow from gs.calendar_date) = 6 and c.saturday = 1)
),

added as (
    select feed_version_date, service_id, date as calendar_date
    from {{ source('silver', 'gtfs_calendar_dates') }}
    where exception_type = 1
),

removed as (
    select feed_version_date, service_id, date as calendar_date
    from {{ source('silver', 'gtfs_calendar_dates') }}
    where exception_type = 2
),

corrected as (
    select feed_version_date, service_id, calendar_date
    from weekly_pattern_expanded
    except
    select feed_version_date, service_id, calendar_date
    from removed
)

select feed_version_date, service_id, calendar_date
from corrected
union
select feed_version_date, service_id, calendar_date
from added
