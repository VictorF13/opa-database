-- Conformed vehicle identity across AFC and GPS: one row per distinct
-- physical vehicle we have ever seen in either source. Where
-- int_vehicle_dictionary confirms a non-ambiguous match, both sides
-- collapse into a single row, using GPS's id_veiculo as the master id
-- since it is the side that is actually unique. Where there is no
-- confirmed match (missing from the dictionary entirely, or only present
-- via an ambiguous cod_veiculo), the vehicle still gets a master id
-- (synthesized, prefixed so it can never collide with a real
-- id_veiculo/vehicle_id value) and is tagged via match_status, rather
-- than being left out or guessed at.
--
-- This is a deterministic function of the current vehicle_dictionary
-- snapshot, not a persisted/stateful surrogate key registry: if the
-- dictionary later gets a confirmed mapping for a vehicle that is
-- currently afc_only or avl_only, simply re-running this model merges
-- the two into one row going forward. No manual remapping step needed.
--
-- afc_only reads vehicle_number from the raw silver.afc_boardings source
-- rather than from dim_afc_trip on purpose: dim_afc_trip's own
-- master_vehicle_id column is resolved via this very model, so going
-- through dim_afc_trip here would be a circular dependency.
with matched as (
    select
        cod_veiculo as afc_vehicle_number,
        id_veiculo as gps_vehicle_id,
        id_veiculo::text as master_vehicle_id,
        'matched' as match_status
    from {{ ref('int_vehicle_dictionary') }}
    where not is_cod_veiculo_ambiguous
),

afc_only as (
    select distinct
        b.vehicle_number as afc_vehicle_number,
        cast(null as integer) as gps_vehicle_id,
        'AFC-' || b.vehicle_number as master_vehicle_id,
        'afc_only' as match_status
    from {{ source('silver', 'afc_boardings') }} as b
    left join matched as m on m.afc_vehicle_number = b.vehicle_number
    where m.afc_vehicle_number is null
),

avl_only as (
    select distinct
        cast(null as text) as afc_vehicle_number,
        a.vehicle_id as gps_vehicle_id,
        a.vehicle_id::text as master_vehicle_id,
        'avl_only' as match_status
    from {{ source('silver', 'avl_pings') }} as a
    left join matched as m on m.gps_vehicle_id = a.vehicle_id
    where m.gps_vehicle_id is null
)

select * from matched
union all
select * from afc_only
union all
select * from avl_only
