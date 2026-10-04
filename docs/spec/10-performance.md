# 10. Performance

Speed is a design input, not a tuning exercise left for later. This
document names the access patterns the research needs, and specifies the
physical design that makes each of them fast, in the lake, in the
compute code, and in the serving database.

## 1. The workload

| ID | Pattern | Example |
| --- | --- | --- |
| W1 | Entity-day slice | All pings of one device on one day |
| W2 | As-of join | For each tap, the device's position at or just before the tap time |
| W3 | Interval containment | Which run contains instant t for device d |
| W4 | Interval overlap | Which runs overlap a trip record's open and close times |
| W5 | Spatial projection | A point's progress along a pattern; nearest stop; point in zone |
| W6 | Spatial and temporal candidate search | Which devices were near this place around this time |
| W7 | Fact to fact | Taps with their run; stop events with their run |
| W8 | Large aggregation | Passenger boardings by route and hour for a year |
| W9 | Dimension lookup | Pattern, stop, and route attributes for a fact |

Every requirement below serves one or more of these.

## 2. The lake

- **PERF-1 (MUST)** Large event tables are physically partitioned by day
  in the lake (W1, W8), declared as a property of the table.
- **PERF-2 (MUST)** Tables declare a sort order of entity, then time,
  which the table format maintains in every file:
  `avl_pings` and `ping_activity` by `(device_id, metric_timestamp,
  ping_seq)`; AFC tables by `(bus_id, boarding_at)` or `(bus_id,
  trip_opened_at)`; run-level tables by `(device_id, start_at)`. One
  entity's rows are contiguous, row-group statistics prune by entity
  (W1), and tables that share a key and order join by merge (W7).
- **PERF-3 (MUST)** Row groups hold 100,000 to 150,000 rows. Files are
  between 100 MB and 1 GB wherever the daily volume allows. Small files
  are merged by scheduled maintenance (`DQ-23`).
- **PERF-4 (MUST)** Columns use the narrowest correct type. Identifiers
  and codes are dictionary-encoded. Column statistics are written.
  Compression is Zstandard.

## 3. Compute

- **PERF-5 (MUST)** As-of joins (W2) use the engines' native operators on
  sorted inputs: DuckDB `ASOF JOIN` or Polars `join_asof` with the entity
  as the `by` key. A hand-written inequality join is a defect.
- **PERF-6 (MUST)** Interval joins (W3, W4) always include an equality on
  the entity and are evaluated per entity-day in sorted order (a merge),
  or with the engine's range-join operator. They are never a cross
  product filtered afterwards.
- **PERF-7 (MUST)** Geometry is prepared once per run and reused:
  patterns as projected coordinate arrays with a spatial index, stops
  with their progress precomputed, zones as prepared polygons.
  Coordinates are projected in bulk. Projection onto a pattern and
  point-in-zone tests (W5) are vectorized over arrays; inner loops that
  cannot be vectorized are compiled (Numba).
- **PERF-8 (MUST)** Work is distributed to a pool of
  `ops.compute_workers` processes. A worker takes a whole day, reads
  that day's rows once, and processes every entity in it, so the number
  of reads is proportional to days, not to entity-days. A month's output
  is committed in one transaction.
- **PERF-9 (MUST)** No transformation iterates over rows in interpreted
  Python. Per-row logic is expressed as columnar operations, SQL, or
  compiled kernels.
- **PERF-10 (MUST)** Candidate search in space and time (W6) uses integer
  grid cells and time buckets joined by equality (a hash join), followed
  by an exact test on the few candidates. Distance predicates are never
  evaluated over all pairs.
- **PERF-11 (MUST)** DuckDB sessions set threads, memory limit, and a
  temporary directory on the FAST tier explicitly (`PLT-30`), so large
  joins spill to fast storage instead of failing.
- **PERF-12 (MUST)** Work is incremental. Only stale partitions are
  recomputed (`ARC-52`).
- **PERF-13 (SHOULD)** Each component reports its throughput (entity-days
  per second) with every materialization, so a slowdown is visible the
  day it appears.

## 4. The serving database

### 4.1 Partitioning

- **PERF-20 (MUST)** Event tables and facts are range-partitioned by
  month, with identical bounds everywhere. They form two families:

| Family | Partition column | Tables |
| --- | --- | --- |
| Fare collection | `service_date` | `silver.afc_trips`, `silver.afc_boardings`, `gold.fact_afc_trip`, `gold.fact_boarding` |
| Observation | `operational_date` | `gold.fact_activity`, `gold.fact_run`, `gold.fact_stop_event`, `gold.fact_block`, `gold.fact_bus_day`, `gold.fact_device_day`, `inference.*` event tables |

  `silver.avl_pings` is partitioned by month of `event_date`.

  Within a family a child row has the same partition date as its parent
  (a tap and its trip record; a stop event and its run). Joins inside a
  family therefore include the partition column, run partition by
  partition, and prune to the months asked for (W7, W8).
- **PERF-21 (MUST)** Primary keys of partitioned tables are the partition
  column plus the identifier, as PostgreSQL requires. Global uniqueness
  of the identifier is guaranteed by construction (`ARC-20`) and
  verified by a check.
- **PERF-22 (MUST)** References inside a family are declared foreign
  keys. References across families (a tap's `run_id`, a trip record's
  `primary_run_id`) are by identifier, validated by a publish check
  (`DQ-10`, A13), and the attributes most often needed from the other
  side are already present on the referencing fact, so the join is
  rarely needed.

### 4.2 Physical order and indexes

- **PERF-23 (MUST)** Partitions are loaded in the lake's sort order
  (`PERF-2`), so physical order correlates with entity and time. This
  makes range scans sequential, keeps indexes compact, and makes block
  range indexes effective.
- **PERF-24 (MUST)** Tables that describe intervals carry a range column
  (`during`) and a GiST index on `(entity, during)`. Containment and
  overlap (W3, W4) are index lookups. The same index backs exclusion
  constraints that make overlapping runs, activities, or links for one
  entity impossible.
- **PERF-25 (MUST)** Quantities that need a spatial or temporal
  computation are stored, not recomputed at query time: progress along
  the pattern, distance along the pattern, boarding stop, run of a tap,
  pattern of a run, and stop times. Most research queries are then plain
  equality joins on identifiers (W5, W7).
- **PERF-26 (MUST)** The index policy is explicit and minimal. An index
  exists because a named workload pattern needs it.

| Table | Index | Serves |
| --- | --- | --- |
| `silver.avl_pings` | Primary key `(event_date, device_id, metric_timestamp, ping_seq)`; block range index on `metric_timestamp`; GiST on `geom` | W1, W2, time ranges, spatial queries |
| `silver.afc_boardings`, `gold.fact_boarding` | Primary key; `(bus_id, boarding_at)`; `(afc_trip_id)`; `(run_id)`; `(card_key, boarding_at)`; `(boarding_stop_id, service_date)`; GiST on `geom` | W1, W7, per-card and per-stop analysis |
| `gold.fact_afc_trip` | Primary key; `(bus_id, trip_opened_at)`; `(route_id_observed, service_date)` | W1, W4 |
| `gold.fact_run` | Primary key; GiST `(device_id, during)`; `(bus_id, start_at)`; `(pattern_id, operational_date)`; `(route_id, operational_date)` | W3, W4, per-route analysis |
| `gold.fact_activity` | Primary key; GiST `(device_id, during)`; `(bus_id, start_at)` | W3 |
| `gold.fact_stop_event` | Primary key `(operational_date, run_id, stop_seq)`; `(stop_id, operational_date)`; `(route_id, operational_date)` | W7, per-stop analysis |
| `gold.fact_bus_device_link` | GiST `(bus_id, during)` and `(device_id, during)` | W3 |
| Dimensions | Primary key; GiST on geometry where present | W9, W5 |

- **PERF-27 (MUST)** A new index is added only with a benchmark showing
  the query it serves and its cost in space and load time. Unused
  indexes found in the weekly review (`OPS-33`) are removed.
- **PERF-28 (MUST)** Row width is kept small: identifiers as 16-byte
  values, enumerations as database enumerated types generated from the
  `ref` vocabularies, flags as booleans or one integer, coordinates as
  double precision, amounts as decimal. Fixed-width columns come first.
- **PERF-29 (MUST)** Published partitions are read-only in practice. They
  are created with a fill factor of 100, loaded with frozen tuples, and
  analyzed at load, so index-only scans work immediately and no later
  rewrite is needed. Extended statistics are created for column groups
  the planner would otherwise assume independent (for example route and
  direction; bus and device).

## 5. Publishing

- **PERF-30 (MUST)** Data moves from the lake to PostgreSQL in binary form
  through the bulk-load protocol, in columnar batches, read at the
  release's snapshot, with no text serialization and no per-row
  statements.
- **PERF-31 (MUST)** A partition is published by building it aside and
  swapping it in:
  1. Create a standalone table with the parent's structure.
  2. Bulk load it in the same transaction that created it, with frozen
     tuples.
  3. Build its indexes with parallel maintenance workers.
  4. Add its constraints, including a check constraint equal to the
     partition bounds so that attaching needs no scan.
  5. Analyze it.
  6. In one short transaction: detach the old partition if any, attach
     the new one, drop the old one.

  Readers are blocked only for the instant of step 6, and a failure
  before it leaves the published data untouched. Dimension tables are
  swapped by rename in one transaction.
- **PERF-32 (MUST)** Partitions are loaded in parallel across several
  connections, bounded by `publish.parallelism` (default 6).
- **PERF-33 (MUST)** Privileges are applied and verified at the end of
  every publish (`SEC-12`).

## 6. Budgets

Budgets are stated for the reference host (`03-platform.md`). They are
initial values: phase P1 measures the platform, P8 measures the full
pipeline, and the table is then updated with what was measured. After
that, a budget is a requirement.

- **PERF-40 (MUST)** Reference operations and budgets:

| Operation | Budget |
| --- | --- |
| Read one device-day of pings from the lake | 100 ms |
| Bronze ingest of one month, all sources | 30 min |
| Silver build of one month, all sources | 30 min |
| Inference for one month, all components, both passes | 3 h |
| Gold assembly and all checks for one month | 30 min |
| Publish one month of gold and inference | 30 min |
| Publish one month of `silver.avl_pings` | 60 min |
| One year, raw to published | 2 days |
| Query: timeline of one bus-day (activities, runs, records, taps) | 200 ms |
| Query: stop events of one route on one day | 1 s |
| Query: position of each of 1,000 taps at tap time | 1 s |
| Query: passenger boardings by route and hour for one month | 5 s |
| Query in the lake: the same for one year | 10 s |

- **PERF-41 (MUST)** `opa bench` runs the reference operations and
  queries against a fixed month and stores the timings in
  `meta.benchmark`. It runs as part of the release gate (`DQ-26`).
- **PERF-42 (MUST)** A release whose benchmark exceeds a budget by more
  than 20% is not published until the regression is explained or the
  budget is revised by pull request.

## 7. How to write the common joins

The documentation includes a query guide (`ENG-57`) with the efficient
form of each pattern. The essentials:

| Pattern | In the lake (DuckDB) | In the serving database |
| --- | --- | --- |
| W1 | Filter on the day partition and the entity | Filter on the partition date and the entity; the primary key serves it |
| W2 | `ASOF JOIN` on entity and time | Lateral subquery ordered by time descending, limit 1, on the entity and time index; or use the stored result |
| W3 | Range join with entity equality | `during @> instant` with entity equality; the GiST index serves it |
| W4 | Range join with entity equality | `during && range` with entity equality |
| W5 | Spatial functions on prepared geometry | Use stored progress and stop columns; fall back to PostGIS on the dimension geometry |
| W7 | Merge join on shared key and order | Join on identifier plus partition date inside a family |
| W8 | Scan of the needed columns over the month partitions | Facts with partition pruning; for whole years, prefer the lake |
