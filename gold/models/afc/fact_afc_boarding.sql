-- One row per boarding event, referencing its trip via trip_id instead of
-- repeating the trip/line/vehicle/company columns on every row. Pairs
-- with dim_afc_trip; together they replace silver.afc_boardings's ~16.4x
-- redundant flat shape with a proper fact/dimension split.
--
-- Does not carry master_vehicle_id directly: vehicle identity is a
-- trip-level attribute, already on dim_afc_trip, reachable via trip_id.
-- Duplicating it here too would be exactly the kind of redundancy the
-- trip/boarding split exists to remove.
select
    b.event_id,
    {{ afc_trip_key(relation_alias='b') }} as trip_id,
    b.boarding_at,
    b.integration_bum,
    b.integration_type,
    b.sigben,
    b.passenger_type,
    b.card_id,
    b.fare_paid,
    b.subsidy_value,
    b.metro_transfer_value,
    b.latitude,
    b.longitude,
    b.geom
from {{ source('silver', 'afc_boardings') }} as b
