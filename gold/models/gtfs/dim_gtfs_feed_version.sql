-- One row per GTFS feed export, with the real-world calendar date range
-- it was actually in effect for. A schedule snapshot stays valid until
-- the next export supersedes it, so valid_to is just the next
-- feed_version_date; the latest export has no upper bound (valid_to is
-- null, meaning "still current").
--
-- Any fact that needs "which schedule applied on date X" should join on
-- `event_date >= valid_from and (valid_to is null or event_date < valid_to)`
-- rather than assuming a specific feed_version_date.
--
-- gtfs_calendar is used only to enumerate the distinct feed_version_dates
-- that exist; every GTFS table shares the same set by construction (one
-- bronze/silver load stamps the same export date across all of them), so
-- any one of them would do equally well here.
select
    feed_version_date as valid_from,
    lead(feed_version_date) over (order by feed_version_date) as valid_to,
    feed_version_date
from (
    select distinct feed_version_date
    from {{ source('silver', 'gtfs_calendar') }}
) as feed_versions
