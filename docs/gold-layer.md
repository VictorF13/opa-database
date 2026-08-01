# Gold layer (dbt)

The gold layer is a [dbt-core](https://docs.getdbt.com/) project in `gold/`
at the repo root (`dbt_project.yml`, `profiles.yml`, `models/`, `macros/`,
`tests/`), a separate SQL-based toolchain from `src/opa_database/`, not
Python package code. It builds normalized, cross-source models on top of
the `silver` schema, materialized into a `gold` schema on the same
Postgres+PostGIS instance.

## Running it

```bash
uv run dbt debug --project-dir gold --profiles-dir gold   # verify the connection
uv run dbt run   --project-dir gold --profiles-dir gold
uv run dbt test  --project-dir gold --profiles-dir gold
```

Connection settings (`gold/profiles.yml`) read from the same
`SILVER_DB_USER`/`SILVER_DB_PASSWORD`/`SILVER_DB_NAME` environment
variables as the rest of the project (see `.env.example`), plus
`SILVER_DB_HOST` (defaults to `localhost`).

### Why Python 3.12, not 3.13/3.14

This project's Python version is pinned to **3.12** in `pyproject.toml`
and `.python-version` specifically because of dbt. `dbt-core` cannot even
import under Python 3.14: its dependency `mashumaro` (JSON-schema
serialization) raises `UnserializableField` on a plain `Optional[str]`
field at class-definition time, on every invocation. This was verified
systemic: the entire `mashumaro` version range `dbt-core` allows (3.9
through 3.14) fails identically under Python 3.14. Python 3.13 hits a
different but related `mashumaro`/`typing_extensions` incompatibility.
**Python 3.12 is confirmed clean.** If this error resurfaces, check
`.python-version`/`requires-python` before assuming it's a real dbt config
problem. Don't try to pin `mashumaro`/`typing_extensions` versions within
dbt-core's allowed range instead, that was tried first and doesn't work.

## Design conventions

- **Composite natural keys everywhere.** Every GTFS and AFC gold table is
  keyed by `(feed_version_date, entity_id)` or similar, never a bare id,
  because ids repeat across different snapshots (the same GTFS `route_id`
  is a *different* real entity in the Nov 10 vs. Nov 20 feed export).
  Because of this, dbt's built-in single-column `relationships` test would
  falsely pass a broken reference as long as the id existed in *any*
  snapshot of the parent table. Referential integrity is instead enforced
  with hand-written singular tests in `gold/tests/*_exists.sql` that join
  on the full composite key. Composite uniqueness is similarly checked
  with hand-written `gold/tests/*_unique_per_*.sql` tests rather than dbt's
  single-column `unique` test.
- **Surrogate keys for entities with no natural id.** The raw AFC feed has
  no trip identifier at all, so `dim_afc_trip.trip_id` is a deterministic
  `md5(concat_ws('|', ...))` hash over the full ancestor chain (service
  date, category, company, vehicle, line, trip timing/turnstiles),
  generated once by the shared macro `macros/afc_trip_key.sql` and reused
  everywhere that key is needed (with an optional table-alias parameter for
  use inside joins).
- **Don't duplicate an id that's reachable through an existing
  relationship.** Fact tables carry the conformed `master_vehicle_id`
  where it belongs at that table's own grain and nowhere else, e.g.
  `fact_afc_boarding` doesn't carry vehicle identity directly, since it's
  already on `dim_afc_trip` and reachable via `trip_id`. `fact_avl_ping`
  omits the raw `vehicle_id` entirely since it's fully recoverable via
  `dim_vehicle_master.gps_vehicle_id`.
- **Ephemeral models for internal-only building blocks.** A model with
  exactly one consumer and no reason to be queried directly (e.g.
  `int_vehicle_dictionary`, prefixed `int_` rather than `dim_`) is
  materialized as `ephemeral` (inlined as a CTE, no table/view object ever
  created) instead of a real table.
- **Referential integrity is dbt tests, not DDL constraints.** No actual
  Postgres `FOREIGN KEY`/`PRIMARY KEY` constraints exist in gold yet. dbt
  supports enforced "model contracts" for this, but it hasn't been turned
  on for anything. Integrity is checked by tests run after a model builds,
  not enforced by the database itself.

## Model inventory

### AFC (`models/afc/`)

Splits silver's flat, ~16.4x-redundant `afc_boardings` into a proper
fact/dimension pair:

- **`dim_afc_trip`**: one row per real trip instance (one `Viagem` in the
  raw XML). Carries `master_vehicle_id` (joined from `dim_vehicle_master`)
  instead of the raw `vehicle_number`, since vehicle identity is a
  trip-level attribute. Also carries `boarding_count`.
- **`fact_afc_boarding`**: one row per boarding event, referencing its
  trip via `trip_id` instead of repeating trip/line/vehicle/company
  columns. Does not carry vehicle identity directly (reachable via
  `trip_id` -> `dim_afc_trip.master_vehicle_id`).

### Vehicle identity (`models/vehicle/`)

Reconciles AFC vehicle identity (`vehicle_number`) and GPS vehicle identity
(`vehicle_id`) into one conformed dimension, since the two sources use
different, independently-assigned ids for the same physical vehicles:

- **`int_vehicle_dictionary`** (ephemeral): a plain crosswalk between
  AFC's `cod_veiculo` and GPS's `id_veiculo` from the *latest* ingested
  `vehicle_dictionary` snapshot only. Flags `is_cod_veiculo_ambiguous` for
  codes reassigned to more than one `id_veiculo` (buses get reassigned
  over time; the raw dictionary itself is not a clean 1:1 mapping).
  Internal-only: nothing outside `dim_vehicle_master` should query this.
- **`dim_vehicle_master`**: one row per distinct physical vehicle seen in
  *either* source. Where the dictionary confirms a non-ambiguous match,
  both sides collapse into one row (`master_vehicle_id` = GPS's
  `id_veiculo`, the side that's actually unique). Where there's no
  confirmed match (missing from the dictionary, or only present via an
  ambiguous code), the vehicle still gets a master id (a synthesized,
  source-prefixed id such as `AFC-<vehicle_number>`, guaranteed never to
  collide with a real `id_veiculo`) and is tagged via `match_status`
  (`matched` / `afc_only` / `avl_only`). Every vehicle from every source
  resolves to exactly one `master_vehicle_id`, never left out. This is a
  **deterministic function of the current `vehicle_dictionary` snapshot**,
  not a persisted/stateful surrogate-key registry: if the dictionary later
  confirms a mapping for a currently-unmatched vehicle, re-running gold
  merges it going forward, with no manual remap step.
- **`fact_avl_ping`**: all of `silver.avl_pings`, keyed by
  `master_vehicle_id` instead of the raw `vehicle_id` (fully recoverable
  via `dim_vehicle_master.gps_vehicle_id`, so not duplicated here).

**Deliberately not done yet**: reconciling `fact_avl_ping.route_code`
against `dim_gtfs_route.route_id`. They might correspond (`route_code` is a
bare int like `4`; `route_id` is a zero-padded string like `"0004"`) but
this is genuinely unverified and risks matching wrong data silently, so
it's left as a known gap rather than guessed at.

### GTFS (`models/gtfs/`)

Every one of the 10 silver GTFS tables has a gold counterpart, so no query
needs to fall back to silver directly:

- **`dim_gtfs_agency`**, **`dim_gtfs_route`**, **`dim_gtfs_trip`**,
  **`dim_gtfs_stop`**, **`dim_gtfs_stop_time`**, **`dim_gtfs_fare`**,
  **`dim_gtfs_fare_rule`**: straightforward per-table pass-throughs from
  their silver sources, keyed by `feed_version_date` plus the table's
  natural id(s). `dim_gtfs_stop_time` is keyed by
  `(feed_version_date, trip_id, stop_sequence)`, not `stop_id`, since a
  trip can revisit the same stop (e.g. a loop route); it's the largest of
  these tables, so it has an index on `(feed_version_date, trip_id)`.
  `dim_gtfs_trip` is named to avoid confusion with `dim_afc_trip` (a real
  observed vehicle run, not a GTFS schedule definition). `dim_gtfs_stop_time`
  also carries `copied_from_feed_version_date`, non-null for the one
  export whose own `stop_times.txt` was missing and substituted from
  another export (see `architecture.md`'s GTFS notes).
- **`dim_shape`**: GTFS shape points aggregated into a single
  `LINESTRING` per `(feed_version_date, shape_id)` via
  `ST_MakeLine(geom ORDER BY shape_pt_sequence)`. Point-level granularity
  stays in `silver.gtfs_shapes`.
- **`dim_gtfs_feed_version`**: one row per feed export, with the
  real-world date range (`valid_from`/`valid_to`) it was actually in
  effect for (`valid_to` is `null` for the current export). Answers "which
  `feed_version_date` applied on real date X" via
  `event_date BETWEEN valid_from AND valid_to` (or `valid_to IS NULL`), so
  nothing has to guess or hardcode a snapshot.
- **`dim_gtfs_service_date`**: one row per
  `(feed_version_date, service_id, calendar_date)` a service actually
  operated on: `gtfs_calendar`'s weekly day-of-week pattern expanded via
  `generate_series` + a lateral join over its date range, then corrected
  by `gtfs_calendar_dates`' exceptions (`exception_type 1` adds a date,
  `2` removes one). Precomputed once so nothing downstream has to redo the
  day-of-week/exception logic itself. Describes one feed snapshot's own
  calendar; pair with `dim_gtfs_feed_version` to know which snapshot
  applied on a given real date. Also carries
  `copied_from_feed_version_date`, non-null for dates whose inclusion
  came from a borrowed `calendar_dates` "added" exception row (see Known
  gaps below for what this can't surface).

## Testing

Run `uv run dbt test --project-dir gold --profiles-dir gold`. Two kinds of
tests exist:

- **Schema tests** (`models/*/_*.yml`): `not_null`, `accepted_values`, and
  dbt's built-in `relationships`/`unique` where a bare single-column check
  is actually sufficient (i.e. not one of the composite-key GTFS/AFC
  cases above).
- **Singular composite-key tests** (`tests/*.sql`): hand-written,
  returning zero rows on success. Two shapes recur:
  - `*_unique_per_feed.sql` / `*_unique_per_day.sql`: composite
    uniqueness (e.g. `dim_gtfs_route_unique_per_feed.sql` checks
    `(feed_version_date, route_id)` is unique, since `route_id` alone
    isn't).
  - `*_exists.sql`: composite referential integrity (e.g.
    `dim_gtfs_trip_route_exists.sql`: a `LEFT JOIN` on
    `(feed_version_date, route_id)` together, asserting no child row's FK
    is non-null while the matching parent is missing).
    `dim_gtfs_stop_time_trip_exists.sql`/`_stop_exists.sql` join on
    `coalesce(copied_from_feed_version_date, feed_version_date)` instead
    of plain `feed_version_date`: a row whose whole `stop_times` table
    was substituted from another export (see `architecture.md`'s GTFS
    notes) carries that donor export's `trip_id`/`stop_id` namespace, not
    its own nominal `feed_version_date`'s — joining on the row's own
    `feed_version_date` would flag every substituted row as a
    false-positive broken reference.

## Known gaps

See the "Known gaps" section of [`architecture.md`](architecture.md). The
gold-specific ones (no AFC+AVL+GTFS combined fact table, no enforced
DDL-level constraints, no dbt run in CI) live there to avoid duplicating
the list in two places.

`dim_gtfs_service_date`'s grain (one row per operating day, not per
`calendar_dates` source row) means `copied_from_feed_version_date` can
only surface provenance for dates *included* via a borrowed "added"
exception. It can't surface the analogous case for a date *excluded* by a
borrowed "removed" exception, since an excluded date produces no row here
to mark at all — full traceability for that case requires querying
`silver.gtfs_calendar_dates` directly.
