{{ config(materialized='ephemeral') }}

-- Internal building block for dim_vehicle_master only, not meant for
-- direct querying (hence "int_", not "dim_"): a plain crosswalk between
-- AFC's vehicle identifier (cod_veiculo) and GPS's vehicle identifier
-- (id_veiculo), from the latest ingested vehicle_dictionary snapshot
-- only, not a union of every historical snapshot. It only covers
-- vehicles present in the raw dictionary file; dim_vehicle_master is the
-- complete picture (it also covers AFC-only and AVL-only vehicles that
-- never appear here), so that's what anything downstream should use.
-- Ephemeral rather than a table: nothing else references this, so there
-- is no reason for it to exist as its own object in the database.
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
