-- One row per real AFC trip instance (one Viagem in the raw XML), instead
-- of the silver layer's one row per boarding on that trip. This is the
-- fact/dimension split explicitly deferred from silver: see
-- silver_gold_normalization_boundary in project memory for why.
select
    {{ afc_trip_key() }} as trip_id,
    dump_date,
    service_date,
    category_type,
    company_code,
    company_modality,
    vehicle_number,
    validator_id,
    line_number,
    line_shift,
    line_operator_number,
    line_fare_table,
    line_opened_at,
    line_closed_at,
    trip_opened_at,
    trip_closed_at,
    turnstile_start,
    turnstile_end,
    direction,
    stop_open,
    stop_close,
    count(*) as boarding_count
from {{ source('silver', 'afc_boardings') }}
group by
    dump_date,
    service_date,
    category_type,
    company_code,
    company_modality,
    vehicle_number,
    validator_id,
    line_number,
    line_shift,
    line_operator_number,
    line_fare_table,
    line_opened_at,
    line_closed_at,
    trip_opened_at,
    trip_closed_at,
    turnstile_start,
    turnstile_end,
    direction,
    stop_open,
    stop_close
