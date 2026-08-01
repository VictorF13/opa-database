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
    -- Grouped (not a plain select) so this is guaranteed at most one row
    -- per (feed_version_date, service_id, calendar_date): it's joined
    -- back below to attach copied_from_feed_version_date, and a fan-out
    -- here would break dim_gtfs_service_date_unique_per_day.
    select
        feed_version_date,
        service_id,
        date as calendar_date,
        max(copied_from_feed_version_date) as copied_from_feed_version_date
    from {{ source('silver', 'gtfs_calendar_dates') }}
    where exception_type = 1
    group by feed_version_date, service_id, date
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
),

service_dates as (
    select feed_version_date, service_id, calendar_date from corrected
    union
    select feed_version_date, service_id, calendar_date from added
)

-- copied_from_feed_version_date is populated only for dates whose
-- presence here is owed to a borrowed calendar_dates "added" exception
-- row (see adapters/gtfs.py's substitute-export fallback). It is NOT
-- populated for dates *excluded* by a borrowed "removed" exception,
-- since an excluded date produces no row here to mark at all -- that
-- fact is only visible directly on
-- silver.gtfs_calendar_dates.copied_from_feed_version_date. Known,
-- deliberate limitation of this table's grain (one row per operating
-- day, not per calendar_dates source row).
select
    sd.feed_version_date,
    sd.service_id,
    sd.calendar_date,
    a.copied_from_feed_version_date
from service_dates as sd
left join added as a
    on  sd.feed_version_date = a.feed_version_date
    and sd.service_id        = a.service_id
    and sd.calendar_date     = a.calendar_date
