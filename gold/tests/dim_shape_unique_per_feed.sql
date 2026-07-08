-- dbt singular test: passes if this returns zero rows.
-- Avoids a dbt_utils dependency for one composite-uniqueness check.
select feed_version_date, shape_id, count(*)
from {{ ref('dim_shape') }}
group by feed_version_date, shape_id
having count(*) > 1
