-- One row per fare, per feed export.
select
    feed_version_date,
    fare_id,
    price,
    currency_type,
    payment_method,
    transfers,
    transfer_duration
from {{ source('silver', 'gtfs_fare_attributes') }}
