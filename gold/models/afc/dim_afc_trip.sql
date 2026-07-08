-- One row per real AFC trip instance (one Viagem in the raw XML), instead
-- of the silver layer's one row per boarding on that trip. This is the
-- fact/dimension split explicitly deferred from silver: see
-- silver_gold_normalization_boundary in project memory for why.
--
-- master_vehicle_id replaces the raw vehicle_number: vehicle identity is
-- a trip-level attribute (one vehicle per trip), so it belongs here, not
-- duplicated onto every row of fact_afc_boarding. The raw vehicle_number
-- is fully recoverable via dim_vehicle_master.afc_vehicle_number for any
-- master_vehicle_id, so nothing is lost, just relocated to where it
-- canonically belongs.
with trips as (
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
)

select
    t.trip_id,
    t.dump_date,
    t.service_date,
    t.category_type,
    t.company_code,
    t.company_modality,
    dvm.master_vehicle_id,
    t.validator_id,
    t.line_number,
    t.line_shift,
    t.line_operator_number,
    t.line_fare_table,
    t.line_opened_at,
    t.line_closed_at,
    t.trip_opened_at,
    t.trip_closed_at,
    t.turnstile_start,
    t.turnstile_end,
    t.direction,
    t.stop_open,
    t.stop_close,
    t.boarding_count
from trips as t
left join {{ ref('dim_vehicle_master') }} as dvm
    on dvm.afc_vehicle_number = t.vehicle_number
