{#
    Surrogate key for one AFC trip instance (one Viagem in the raw XML):
    the natural key is the whole ancestor chain (service date, category,
    company, vehicle, line, trip timing/turnstiles), since there's no
    single natural id for a trip in the raw feed. All inputs are declared
    NOT NULL in silver.afc_boardings, so concat_ws needs no null handling.

    relation_alias: optional table alias/prefix (e.g. 'b' for 'b.column'),
    used when the columns are qualified in a join.
#}
{% macro afc_trip_key(relation_alias='') %}
{%- set prefix = relation_alias ~ '.' if relation_alias else '' -%}
md5(
    concat_ws(
        '|',
        {{ prefix }}dump_date,
        {{ prefix }}service_date,
        {{ prefix }}category_type,
        {{ prefix }}company_code,
        {{ prefix }}company_modality,
        {{ prefix }}vehicle_number,
        {{ prefix }}validator_id,
        {{ prefix }}line_number,
        {{ prefix }}line_shift,
        {{ prefix }}line_operator_number,
        {{ prefix }}line_fare_table,
        {{ prefix }}line_opened_at,
        {{ prefix }}line_closed_at,
        {{ prefix }}trip_opened_at,
        {{ prefix }}trip_closed_at,
        {{ prefix }}turnstile_start,
        {{ prefix }}turnstile_end,
        {{ prefix }}direction,
        {{ prefix }}stop_open,
        {{ prefix }}stop_close
    )
)
{% endmacro %}
