# 05. Silver

Silver turns bronze text into typed, cleaned, deduplicated tables with
consistent keys, one table per source entity. It is the first layer meant
to be queried, and the only input (with reference data) to inference.

Silver corrects form, not substance. It parses, types, normalizes keys,
flags sentinels, and removes exact duplicates. It does not join sources
and it does not decide which records are true.

## 1. Rules

- **SLV-1 (MUST)** Silver has one table per source entity (section 3).
- **SLV-2 (MUST)** Event tables are partitioned by the date on which the
  event happened, not by delivery date. Snapshot tables (schedule,
  dictionaries) are partitioned by snapshot date.
- **SLV-3 (MUST)** The unit of build is one partition of one table. Its
  inputs are all bronze files that the coverage index (`BRZ-9`) lists for
  that partition. The partition is written as a single file and replaced
  atomically (`ARC-12`).
- **SLV-4 (MUST)** A partition is rebuilt when any contributing bronze
  file, the transform version, the relevant parameters, or the relevant
  reference data change (`ARC-52`). A newly arrived raw file that covers
  an already built date makes that date stale (`ARC-55`).
- **SLV-5 (MUST)** Every table has a schema contract: column names,
  types, nullability, allowed ranges, and the primary key. The contract is
  a Pandera schema in code, validated on every build.
- **SLV-6 (MUST)** Every bronze row has exactly one disposition, recorded
  per partition in `meta.silver_partition`:

  ```text
  bronze rows = silver rows + rejected rows + duplicate rows
  ```

  - A **silver row** may carry quality flags.
  - A **rejected row** could not be given a primary key or an event time
    (for example an unparseable timestamp). It is written to
    `silver/_rejects/<table>/` with a reason code.
  - A **duplicate row** repeats a row already kept (`SLV-12`). It is
    counted, and listed in `silver/_rejects/` with the reason `duplicate`
    and a pointer to the kept row.
- **SLV-7 (MUST)** Every silver row keeps `_file_id` and `_row`, so the
  original text is one lookup away.
- **SLV-8 (MUST)** Keys are canonicalized as in `ARC-25`, and the source
  value is kept beside the canonical one.
- **SLV-9 (MUST)** Instants are UTC timestamps. Naive local timestamps are
  localized to `America/Fortaleza` and converted. The three date columns
  of `ARC-30` are added to every event table.
- **SLV-10 (MUST)** Known sentinel values are replaced by null and a
  quality flag is set. Sentinels are data (`ref.sentinel`), not code.
- **SLV-11 (MUST)** Each event table has a `quality_flags` integer whose
  bits are defined in `ref.quality_flag`. A flag describes the row; it
  never removes the row.
- **SLV-12 (MUST)** Deduplication is defined per table (section 3). The
  kept row is the one from the earliest delivered file, then the lowest
  `_row`. When duplicates disagree in any field, the kept row gets the
  flag `conflicting_duplicate`.
- **SLV-13 (MUST)** Text is trimmed and normalized to Unicode NFC. An
  empty string becomes null except where the contract says an empty value
  is meaningful.
- **SLV-14 (MUST)** Monetary amounts are exact decimals with two fraction
  digits, never floating point.
- **SLV-15 (MUST)** A coordinate is valid when both values parse, are not
  the no-fix marker, and are within the valid ranges. A valid coordinate
  outside the service-area box (`geo.area_bbox`) is kept and flagged
  `out_of_area`. An invalid coordinate becomes null and is flagged.
- **SLV-16 (MUST)** Silver does not join one source to another and does
  not use labels or models.
- **SLV-17 (MUST)** Primary keys are unique. A violation fails the build
  of that partition.
- **SLV-18 (MUST)** Within a partition, rows are sorted as section 3
  states, so that one entity's rows are contiguous (`PERF-2`).

## 2. Partitioning and time

| Table | Partition column | Meaning |
| --- | --- | --- |
| `avl_pings` | `event_date` | UTC date of the ping |
| `afc_trips`, `afc_boardings` | `service_date` | The agency's service date |
| `gtfs_*` | `feed_date` | Date of the schedule export |
| `identity_claims` | `snapshot_date` | Date of the dictionary snapshot |

`afc_*` tables also keep `dump_date`, the delivery date of the file each
row came from, because lateness is itself information.

## 3. Tables

### 3.1 `avl_pings`

One row per GPS ping.

| Column | Type | Notes |
| --- | --- | --- |
| `device_id` | text | Canonical |
| `metric_timestamp` | timestamp with time zone | Source field `metrictimestamp` |
| `ping_seq` | small integer | Order among pings of one device with the same timestamp, starting at 0 |
| `vehicle_id` | big integer | The AVL system's internal vehicle identifier |
| `latitude`, `longitude` | double precision | Null when invalid |
| `heading_deg` | small integer | Source field `direction`, 0 to 359; null when out of range |
| `speed` | small integer | Unit recorded in the data dictionary after P0 |
| `odometer` | big integer | Unit recorded in the data dictionary after P0 |
| `route_code` | integer | Null when the source value is 0 (unset) |
| `route_id` | text | Canonical form of `route_code` |
| `event_date`, `local_date`, `operational_date` | date | `ARC-30` |
| `quality_flags` | integer | |
| `_file_id`, `_row` | | Lineage |

- Primary key: `(device_id, metric_timestamp, ping_seq)`.
- Sort: `device_id`, `metric_timestamp`, `ping_seq`.
- Duplicate: two rows equal in every source field.
- Rejected: unparseable timestamp; empty device identifier.
- Flags: `no_fix`, `out_of_area`, `heading_invalid`, `speed_invalid`,
  `route_unset`, `same_timestamp` (shares its timestamp with another ping
  of the device), `conflicting_duplicate`, `outside_file_window` (the
  timestamp is far from the delivery period of its file).

- **SLV-20 (MUST)** The source field named `direction` is exposed only as
  `heading_deg`. It is a compass heading and must not be confused with a
  route direction.
- **SLV-21 (MUST)** `ping_seq` is assigned deterministically: by
  timestamp, then by delivery order of the file, then by `_row`.

### 3.2 `afc_trips`

One row per operator trip record (`Viagem`), whether or not it has taps.

| Column | Type | Notes |
| --- | --- | --- |
| `afc_trip_id` | identifier | `ARC-21` |
| `service_date` | date | As recorded |
| `company_code`, `company_id` | text | Source and canonical |
| `company_modality`, `category_type` | small integer | As recorded |
| `vehicle_number`, `bus_id` | text | Source and canonical |
| `validator_id` | text | Nullable |
| `line_number`, `route_id` | text | Source and canonical |
| `line_shift`, `line_operator_number`, `line_fare_table` | | Line session attributes |
| `line_opened_at`, `line_closed_at` | timestamp with time zone | |
| `trip_opened_at`, `trip_closed_at` | timestamp with time zone | Close time null when the source holds the "never recorded" marker |
| `turnstile_start`, `turnstile_end` | integer | |
| `direction_code` | small integer | Source value, 0 or 1 |
| `direction_declared` | text | `I` or `V`, from `direction_code` through `ref.route_direction_rule` |
| `stop_open`, `stop_close` | text | |
| `n_boardings_source` | integer | Child tap elements counted in the source |
| `first_dump_date`, `n_source_occurrences` | | Delivery facts |
| `local_date`, `operational_date` | date | From `trip_opened_at` |
| `quality_flags` | integer | |
| `_file_id`, `_row` | | Lineage of the kept occurrence |

- Primary key: `afc_trip_id`.
- Sort: `bus_id`, `trip_opened_at`.
- Duplicate: same natural key in more than one dump. The occurrences are
  merged into one row.
- Flags: `close_time_missing`, `closes_before_open`, `zero_boardings`,
  `conflicting_duplicate`, `line_session_inconsistent`.

- **SLV-30 (MUST)** A trip record with no taps is a row in `afc_trips`.
  Trips are never derived by grouping taps.

### 3.3 `afc_boardings`

One row per fare tap (`Passageiro`).

| Column | Type | Notes |
| --- | --- | --- |
| `boarding_id` | identifier | `ARC-21` |
| `afc_trip_id` | identifier | Parent trip record |
| `service_date`, `dump_date` | date | |
| `boarding_at` | timestamp with time zone | |
| `event_id` | text | Null when the source holds the placeholder |
| `card_id` | text | Personal data, restricted (`SEC-20`) |
| `card_key` | identifier | Pseudonym (`SEC-21`); null for placeholder cards |
| `passenger_type`, `integration_type`, `integration_bum`, `sigben` | small integer | As recorded; meanings in `ref.afc_code` |
| `fare_paid`, `subsidy_value`, `metro_transfer_value` | decimal(10,2) | |
| `latitude`, `longitude` | double precision | Null when invalid |
| `bus_id`, `company_id`, `route_id`, `direction_code` | | Trip context repeated on each tap |
| `local_date`, `operational_date` | date | |
| `quality_flags` | integer | |
| `_file_id`, `_row` | | Lineage |

- Primary key: `boarding_id`.
- Sort: `bus_id`, `boarding_at`.
- Duplicate: same non-placeholder `event_id` in more than one dump.
- Flags: `event_id_missing`, `card_placeholder`, `no_fix`, `out_of_area`,
  `outside_trip_interval` (tap time outside its trip record's open and
  close times), `conflicting_duplicate`.

- **SLV-31 (MUST)** Trip context is repeated on every tap deliberately.
  In columnar storage the repetition costs almost nothing, and it removes
  a join from the most common queries. The trip-level table remains the
  place where trip attributes are authoritative.
- **SLV-32 (MUST)** Placeholder card and event values are data
  (`ref.sentinel`), established by profiling, not guessed in code.
- **SLV-33 (MUST)** Source elements that are neither trips nor taps
  (`BRZ-7`) are published as `afc_unmapped_elements` so that they are
  visible and counted.

### 3.4 `gtfs_*`

One table per GTFS member file, plus `gtfs_feeds` with one row per export
(feed date, source file, members present, row counts, content digest,
validation summary, substituted members).

- **SLV-40 (MUST)** Identifiers are text and keep their leading zeros.
- **SLV-41 (MUST)** Times of day are kept as text and also as integer
  seconds from the start of the service day (`arrival_s`, `departure_s`),
  which may exceed 86,400.
- **SLV-42 (MUST)** `gtfs_trips` gains a `direction` column (`I`, `V`, or
  null) derived from `direction_id` when present, otherwise from the
  shape identifier through `ref.gtfs_direction_rule`.
- **SLV-43 (MUST)** Each table enforces its natural key within a feed:
  `(trip_id, stop_sequence)` for stop times, `(shape_id,
  shape_pt_sequence)` for shapes, `stop_id`, `trip_id`, `route_id`, and
  so on. Violations are flagged and reported; rows are not dropped.
- **SLV-44 (MUST)** When an export lacks a required member, silver fills
  that table from the nearest export that has it (the earlier one on a
  tie), sets `substituted_from_feed_date` on every such row, and lists
  the member in `gtfs_feeds`. A substitution is never silent.
- **SLV-45 (MUST)** Referential checks between GTFS tables (trips to
  routes, shapes, and services; stop times to trips and stops) run on
  every feed. Results are stored per feed and set flags on the offending
  rows.
- **SLV-46 (MUST)** `gtfs_feeds.same_content_as` records when an export is
  identical in content to an earlier one, so that later layers do not
  treat it as a schedule change.
- **SLV-47 (MUST)** Duplicate-encoding members (a second copy of a table
  in another encoding) are compared with the primary member. Differences
  are reported; no second silver table is created.

### 3.5 `identity_claims`

One row per claim, from any dictionary, that a fleet number corresponds to
an AVL vehicle or device.

| Column | Notes |
| --- | --- |
| `claim_source` | Which dictionary |
| `snapshot_date` | When the dictionary was captured |
| `bus_id`, `bus_code_source` | Canonical and source fleet number |
| `avl_vehicle_id`, `device_id` | Whichever the dictionary provides |
| `company_name`, `plate`, `status` | When provided |
| `attributes` | All remaining source columns, as a map |
| `_file_id`, `_row` | Lineage |

- **SLV-50 (MUST)** Claims are kept as claims. Contradictions between
  dictionaries are preserved, not resolved. Resolution is the job of
  linkage (`INF-30`).

## 4. Publication

- **SLV-60 (MUST)** Silver tables are published to the `silver` schema for
  the configured window (`PLT-50`) with the same names and columns, plus a
  generated point geometry where a table has coordinates.
- **SLV-61 (MUST)** `card_id` is readable only through the restricted
  role (`SEC-20`). Every other role sees `card_key`.

## 5. Acceptance

Silver is accepted for a period when:

1. `SLV-6` holds for every partition, and totals reconcile with bronze.
2. Every contract validates and every primary key is unique.
3. A rebuild changes no output bytes.
4. The reject and flag reports have been reviewed, and every reject
   reason and flag that occurs is documented in the data dictionary.
