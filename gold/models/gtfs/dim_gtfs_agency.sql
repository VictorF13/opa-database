-- One row per transit agency, per feed export.
select
    feed_version_date,
    agency_id,
    agency_name,
    agency_url,
    agency_timezone,
    agency_lang,
    agency_phone,
    agency_fare_url
from {{ source('silver', 'gtfs_agency') }}
