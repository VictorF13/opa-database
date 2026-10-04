# 08. Gold

Gold is the analysis-ready model: conformed dimensions and facts that
answer research questions directly, with the joins already resolved. It
is assembled from silver, inference, and reference data. It computes no
new inference.

## 1. Rules

- **GLD-1 (MUST)** Gold is a dimensional model. Tables are named
  `dim_<entity>`, `fact_<event>`, `bridge_<relation>`, and
  `agg_<subject>_<grain>`, in snake case, with singular entity names.
  Each table is a dbt model.
- **GLD-2 (MUST)** Gold is complete. `fact_boarding` has one row for
  every tap in silver and `fact_afc_trip` one row for every trip record,
  whatever their category. Filtering is the reader's choice, made easy
  by views (section 5), never a property of the tables.
- **GLD-3 (MUST)** Keys are the deterministic identifiers of `ARC-20`.
  Primary keys, uniqueness, and foreign keys are declared in the serving
  database wherever PostgreSQL partitioning allows, and every reference
  is validated on every publish (`PERF-21`, `PERF-22`). A publish with a
  failed constraint or reference check does not complete.
- **GLD-4 (MUST)** Dimensions are conformed: every fact that refers to a
  bus, device, route, pattern, stop, zone, company, date, or card uses
  the same dimension and the same key.
- **GLD-5 (MUST)** Facts are partitioned by month on their date key, with
  identical partition bounds across all facts (`PERF-20`).
- **GLD-6 (MUST)** Enumerated columns hold codes defined in `ref`
  (`REF-6`). In the serving database they are enumerated types generated
  from the `ref` vocabularies, which keeps them readable, compact, and
  constrained (`PERF-28`).
- **GLD-7 (MUST)** Column names carry their unit: `_m` (meters), `_s`
  (seconds), `_kmh`, `_frac` (0 to 1), `_at` (instant), `_date`.
- **GLD-8 (MUST)** Every table and every column has a description in its
  dbt model stating meaning, unit, and origin. The descriptions are
  published as the data dictionary (`ENG-56`) and written as comments on
  the served tables. A model with a missing description fails continuous
  integration (`ENG-61`).
- **GLD-9 (MUST)** Dimensions whose attributes change over time keep
  history with validity intervals (`valid_from`, `valid_to`). A fact
  always joins to the version valid on its date.
- **GLD-10 (MUST)** Gold contains no raw card identifier. Cards appear
  only as `card_key` (`SEC-21`).
- **GLD-11 (MUST)** Declared and observed values are both present
  wherever inference offers a correction (`INF-70`). Column names say
  which is which (`route_id_declared`, `route_id_observed`).
- **GLD-12 (MUST)** Spatial columns are stored as geometry in EPSG:4326.
  Quantities that would otherwise need a spatial computation at query
  time (progress along a pattern, distance to a stop) are stored as
  plain numbers (`PERF-25`).

## 2. Dimensions

| Table | Grain | Key | Main attributes |
| --- | --- | --- | --- |
| `dim_date` | Calendar day | `date` | Year, month, ISO week, day of week, day type (weekday, Saturday, Sunday, holiday), holiday name |
| `dim_company` | Operating company | `company_id` | AFC code, name, modality, whether it has an AVL feed in each period |
| `dim_bus` | Bus | `bus_id` | Company, first and last date seen, source forms of the fleet number |
| `dim_device` | AVL device | `device_id` | Identifier family, AVL vehicle identifiers seen, first and last date seen |
| `dim_zone` | Garage or terminal | `zone_id` | Kind, name, company, terminal kind, polygon, validity |
| `dim_route` | Route | `route_id` | Short name, long name, first and last schedule date |
| `dim_stop` | Stop | `stop_id` | Current name and location |
| `dim_stop_version` | Stop over time | `stop_id`, `valid_from` | Name and location valid in the interval |
| `dim_pattern` | Pattern | `pattern_id` | Route, direction, source (`schedule`, `observed`), parent pattern, corrected pattern, schedule shape identifier, validity, length, stop count, quality, line geometry, scheduled trips per day type |
| `bridge_pattern_stop` | Stop within a pattern | `pattern_id`, `stop_seq` | Stop, progress fraction, distance along the pattern |
| `dim_card` | Fare card | `card_key` | First and last date seen |
| `dim_fare_code` | Coded fare value | `code_family`, `code` | Label, fare class, meaning status |

Vocabulary tables (activity types, statuses, reasons) are the `ref`
tables themselves; gold does not copy them.

## 3. Facts

### 3.1 `fact_afc_trip`

Grain: one operator trip record. Partition: `service_date`.

| Column group | Columns |
| --- | --- |
| Identity | `afc_trip_id`, `service_date`, `operational_date` |
| Who | `bus_id`, `company_id`, `device_id` (linked, nullable), `link_confidence` |
| Declared | `route_id_declared`, `direction_declared`, `trip_opened_at`, `trip_closed_at`, line session attributes, turnstile counters |
| Observed | `route_id_observed`, `direction_observed`, `pattern_id_observed`, `observed_start_at`, `observed_end_at` |
| Reconciliation | `reconciliation_status`, `service_class`, `service_confidence`, `primary_run_id`, `n_runs`, `overlap_group_id` |
| Flags | `route_mismatch`, `direction_mismatch`, `overlaps_other_record`, `close_time_missing`, `opened_in_garage`, `opened_late`, `closed_late` |
| Measures | `n_boardings`, `n_passenger_boardings`, `fare_paid_total`, `duration_s` |

### 3.2 `fact_boarding`

Grain: one fare tap. Partition: `service_date`.

| Column group | Columns |
| --- | --- |
| Identity | `boarding_id`, `afc_trip_id`, `service_date`, `operational_date`, `boarding_at` |
| Who | `bus_id`, `company_id`, `card_key` |
| Fare | `passenger_type`, `integration_type`, `integration_bum`, `fare_class`, `fare_paid`, `subsidy_value`, `metro_transfer_value` |
| Assignment | `run_id` (nullable), `assignment_reason`, `pattern_id`, `route_id_observed`, `direction_observed` |
| Where | `latitude`, `longitude`, `geom`, `position_source`, `boarding_stop_id`, `boarding_stop_seq`, `boarding_stop_method`, `boarding_stop_confidence`, `progress_frac` |

### 3.3 `fact_activity`

Grain: one activity of one device. Partition: `operational_date`.

Columns: `activity_id`, `device_id`, `bus_id` (nullable),
`operational_date`, `activity_type`, `start_at`, `end_at`, `during` (time
range), `zone_id` (nullable), `run_id` (nullable), `block_id`,
`n_pings`, `distance_m`, `duration_s`.

Together with gaps, the activities of a device cover its whole timeline.

### 3.4 `fact_run`

Grain: one run. Partition: `operational_date`.

| Column group | Columns |
| --- | --- |
| Identity | `run_id`, `operational_date`, `track_source` (`avl`, `taps`) |
| Who | `device_id` (nullable for tap-derived runs), `bus_id` (nullable) |
| What | `pattern_id`, `route_id`, `direction` |
| When | `start_at`, `end_at`, `during`, `duration_s` |
| Extent | `first_stop_seq`, `last_stop_seq`, `coverage_frac`, `distance_m`, `completeness_class`, `termination_reason` |
| Quality | `n_pings`, `max_gap_s`, `off_pattern_frac`, `confidence` |
| Service | `service_evidence`, `record_status` (`has_record`, `shared_record`, `no_record`), `n_boardings`, `n_passenger_boardings` |
| Sequence | `block_id`, `previous_run_id`, `next_run_id` |
| Schedule | `scheduled_duration_s` (for the pattern and hour, nullable) |

### 3.5 `fact_stop_event`

Grain: one stop of one run. Partition: `operational_date`.

Columns: `run_id`, `stop_seq`, `operational_date`, `pattern_id`,
`stop_id`, `bus_id`, `route_id`, `direction`, `status`, `arrival_at`,
`departure_at`, `dwell_s`, `method`, `confidence_grade`,
`bracket_gap_s`, `n_boardings`.

Primary key: `(run_id, stop_seq)`. Every stop of the run's pattern has a
row, whatever its status.

### 3.6 `fact_block`

Grain: one garage exit to garage return of one device. Columns:
`block_id`, `device_id`, `bus_id`, `operational_date`, `pull_out_at`,
`pull_in_at`, `garage_out_zone_id`, `garage_in_zone_id`, `n_runs`,
`service_distance_m`, `deadhead_distance_m`.

### 3.7 `fact_bus_device_link`

Grain: one bus, one device, one date interval. Columns: `bus_id`,
`device_id`, `valid_from`, `valid_to`, `during` (date range), `method`,
`confidence`, `n_days_with_evidence`, `evidence` (structured summary).
Intervals do not overlap per bus or per device.

### 3.8 `fact_bus_day` and `fact_device_day`

Grain: one bus (or device) on one operational day. These are the
accountability tables: one row for every bus that had any trip record or
tap, and every device that had any ping.

`fact_bus_day`: link status and unlinked reason; counts of trip records
by reconciliation status and service class; counts of taps by fare class
and assignment reason; runs, full runs, service distance, service time;
first and last activity times.

`fact_device_day`: ping count, noise count, activity counts and
durations by type, runs, linked bus, device class.

## 4. Summary tables

The facts are the product (`00-overview.md`, section 1). No summary table
is required at the start. One is added when a recurring question
justifies it.

- **GLD-20 (MUST)** A summary table is added only with a stated question
  it answers, a definition in terms of the facts, and a test that it
  reconciles with them (`DQ-11`). It is a dbt model in the lake, not a
  database view.

## 5. Views for common use

- **GLD-30 (MUST)** Views give the usual selections a name, so most
  analysts never handle status codes:

| View | Selection |
| --- | --- |
| `v_service_trip` | Trip records with `service_class = revenue_service`, with one effective route, direction, and interval |
| `v_passenger_boarding` | Taps with `fare_class = passenger`, with their run and boarding stop |
| `v_run_service` | Runs with service evidence |
| `v_bus_day_timeline` | For a bus and day: its activities, runs, trip records, and taps on one timeline |
| `v_headway` | For each timed stop event: the time since the previous vehicle of the same route and direction at that stop |

- **GLD-31 (MUST)** Wherever a view offers one "effective" value where
  both a declared and an observed value exist, it uses the observed
  value when present and the declared one otherwise, exposes a column
  naming which was used, and says so in its comment.
- **GLD-32 (MUST)** Views are part of the published interface: defined in
  the serving database's migrations, tested against the facts
  (`DQ-11`), and documented like tables.

## 6. Pings

- **GLD-40 (MUST)** Gold has no ping-level table. Ping-level data is
  `silver.avl_pings` joined with `inference.ping_activity` on the ping
  key, in the lake (always) and in the serving database (for the
  published window, `PLT-50`). The join key and sort order are the same
  in both, so the join is a merge (`PERF-2`).

## 7. Release identity

- **GLD-50 (MUST)** `gold.release_info` exposes the identifier of the
  published release, its date, the months it covers, and the versions it
  pins (`ARC-43`). Every analysis can and should record it.

## 8. Acceptance

Gold is accepted for a release when every constraint validates, every
accounting invariant holds (`DQ-10`), every table and column is
commented, and the performance budgets for the reference queries are met
(`PERF-40`).
