-- Crosswalk between AFC's vehicle identifier (cod_veiculo, matches
-- fact_afc_boarding's vehicle via dim_afc_trip.vehicle_number) and GPS's
-- vehicle identifier (id_veiculo, matches silver.avl_pings.vehicle_id),
-- from the latest ingested vehicle_dictionary snapshot only, not a union
-- of every historical snapshot.
--
-- cod_veiculo is not a reliable unique key: buses get reassigned over
-- time, so some codes map to more than one id_veiculo within the same
-- snapshot. Rather than silently picking one, is_cod_veiculo_ambiguous
-- flags those rows so a consumer can decide how to handle them.
--
-- id_veiculo is confirmed always numeric (verified against the real
-- data), so it is safe to cast here to match avl_pings.vehicle_id's
-- integer type. cod_veiculo is not: it includes non-numeric values (e.g.
-- "DES 02002", "02008 - Desativado", "02018v"), so it stays text, same as
-- fact_afc_boarding's vehicle_number.
with latest_snapshot as (
    select max(snapshot_date) as snapshot_date
    from {{ source('silver', 'vehicle_dictionary') }}
)

select
    v.cod_veiculo,
    v.id_veiculo::integer as id_veiculo,
    v.snapshot_date,
    count(*) over (partition by v.cod_veiculo) > 1 as is_cod_veiculo_ambiguous
from {{ source('silver', 'vehicle_dictionary') }} as v
inner join latest_snapshot as ls on v.snapshot_date = ls.snapshot_date
