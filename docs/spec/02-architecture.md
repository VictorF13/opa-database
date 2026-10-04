# 02. Architecture

## 1. System context

```text
  Remote raw store (read-only)
            |
            |  fetch, verify checksum
            v
+------------------------ BULK tier: raw files and the lake ----------------+
|                                                                           |
|  raw files --> bronze --> silver --> inference --> gold                   |
|  as            lossless   typed,     observed      analysis-ready         |
|  received      text       cleaned,   timelines,    dimensional model      |
|                tables     conformed  links, runs                          |
|                                                                           |
|  one transactional table format; one catalog; versioned by snapshot       |
+---------------------------------------------------------------------------+
     ^                |
     |                |  publish a release: bulk load, constraints,
     |                v  indexes, grants
     |   +---------------- FAST tier: PostgreSQL + PostGIS -----------------+
     |   |  silver | inference | gold | ref | meta   (served copies)        |
     |   |  labels | feedback | ops                  (written here)         |
     |   +------------------------------------------------------------------+
     |                ^                         ^
     |                |                         |
  orchestrator    analysts                 applications
  (runs every     (SQL clients)            (labeling, explorer)
   step, tracks
   every result)
```

The lake holds the data. PostgreSQL serves it and holds the small amount of
state that people write directly. An orchestrator runs every step and
records every result.

## 2. Layers

| Layer | Job | Input | Built with | Never does |
| --- | --- | --- | --- | --- |
| Raw | Keep the original files exactly as received | Remote raw store | A file mirror and a manifest | Modify or delete a file |
| Bronze | A lossless copy of raw in one uniform format, with lineage | Raw | Python parsers | Clean, type, drop, or merge |
| Silver | Typed, cleaned, deduplicated, conformed tables, one per source entity | Bronze, reference | dbt models | Join sources or infer facts |
| Inference | Facts that no source states: links, timelines, patterns, reconciliation | Silver, reference, labels, released models | dbt models where the logic is set-based; Python where it is sequential | Overwrite a source value |
| Gold | The analysis-ready model | Silver, inference, reference | dbt models | Compute new inference |

- **ARC-1 (MUST)** Each layer has exactly the job in the table. Work that
  belongs to a later layer is not done earlier, and the reverse.
- **ARC-2 (MUST)** A layer reads only from layers to its left, from
  reference data, and, for inference, from labels and released models.
  Nothing reads the serving database to compute data, with the single
  exception of labels and feedback, which originate there (`ARC-5`).
- **ARC-3 (MUST)** Inference is part of the silver tier in the
  conventional three-tier reading of this architecture: it is cleaned,
  conformed, entity-level data. It is named separately because its rows
  are estimates with a confidence, not transcriptions of a source.

## 3. Storage roles

- **ARC-4 (MUST)** The lake is the system of record for all data in the
  bronze, silver, inference, and gold layers, for reference data as
  loaded, and for the pipeline's own records (`meta`).
- **ARC-5 (MUST)** The serving database is the system of record only for
  what people and operations write directly: `labels`, `feedback`, and
  `ops`. That state is exported to the lake every day (`BKP-4`).
- **ARC-6 (MUST)** Every other table in the serving database is a copy of
  a lake table at the published release. It can be dropped and reloaded
  from the lake by one command (`OPS-6`) with no loss.
- **ARC-7 (MUST)** Computation that transforms data runs outside the
  serving database, on the lake. The serving database runs queries from
  analysts and applications, and the bulk loads that publish a release.

Rationale: telemetry is append-only and columnar by nature. In columnar
files it is roughly ten times smaller than as database rows with indexes,
one vehicle-day is one contiguous read, and the work parallelizes cleanly
across cores. Keeping the served tables disposable also removes the most
dangerous class of operator mistake.

## 4. The lake

- **ARC-10 (MUST)** The lake uses one open, transactional table format
  for every table: DuckLake. Data is stored as Parquet files with
  Zstandard compression under `${OPA_BULK_ROOT}/lake/`. The catalog (table
  definitions, file lists, statistics, snapshots) is a PostgreSQL
  database (`ARC-17`).
- **ARC-11 (MUST)** Every materialization of a table or partition is
  recorded with the code version, the parameter digest, the reference
  data version, the model releases used, the time, and the row count,
  both in the orchestrator's history and in the lake (`DQ-1`).
- **ARC-12 (MUST)** Writes are transactions. Replacing a partition, or
  the rows of one raw file, either happens completely or not at all. A
  reader never sees a partial state.
- **ARC-13 (MUST)** Several processes may write to the lake at once. The
  orchestrator guarantees that no two runs materialize the same table
  partition at the same time.
- **ARC-14 (MUST)** Every committed change creates a snapshot of the whole
  lake. The latest snapshot is the working state. Earlier states are
  reachable by snapshot for as long as they are retained (`DQ-22`).

| Lake schema | Content |
| --- | --- |
| `bronze` | One table per kind of raw record, plus the quarantine |
| `silver` | Typed tables per source entity, plus their rejects |
| `inference` | Inferred entities and their evidence |
| `gold` | Dimensions, facts, and views |
| `ref` | Reference data, loaded from the repository |
| `meta` | Raw manifest, ingest records, accounting, evaluation, releases |

Why a table format: it provides atomic replacement of a partition,
several writers, file statistics for pruning, declared partitioning and
sort order, and consistent snapshots of all tables. Each of those would
otherwise be code this project has to write and maintain. Why this one:
it is built for the single-host, DuckDB-centered stack used here, keeps
its catalog in a database this system already runs, and stores plain
Parquet. Its use is validated in phase P1 (`17-roadmap.md`); the
fallback, recorded as a decision, is partitioned Parquet written by dbt
with a release manifest kept in `meta`.

## 5. Compute and orchestration

| Tool | Used for |
| --- | --- |
| DuckDB | The query engine over the lake: every SQL transformation, joins, as-of joins, aggregation, ad hoc analysis |
| dbt | Defines, orders, tests, and documents every SQL transformation (silver, set-based inference, gold) and loads reference data |
| Python with Polars, NumPy, Shapely, pyproj, and Numba | Parsing raw files (bronze) and sequential per-vehicle algorithms (track segmentation, map matching, stop events) |
| Dagster | Orchestration: one graph of all tables, partitions, dependencies, schedules, checks, run history |
| PostgreSQL with PostGIS | Serving queries; constraints; application state |

- **ARC-8 (MUST)** The unit of parallel work in Python components is an
  entity-day (one device or one bus on one operational day). Units are
  independent and are processed by a pool of worker processes
  (`PERF-8`).
- **ARC-9 (MUST)** Every table in the lake is an asset of the
  orchestrator: it has declared upstream assets, a partitioning, a code
  version, and checks. There is no step that the orchestrator does not
  know about.
- **ARC-18 (MUST)** Logic that is a set-based transformation is written
  as a dbt model in SQL. Logic that walks a sequence and carries state
  from one element to the next is written in Python. Neither is used
  where the other fits.
- **ARC-19 (MUST)** A capability that a chosen tool already provides is
  used as provided. Project code does not reimplement ordering,
  incremental processing, staleness tracking, testing, documentation,
  scheduling, retries, or run history.

## 6. Databases

- **ARC-17 (MUST)** The PostgreSQL instance holds three databases with
  separate roles (`SEC-1`):

| Database | Content | Can be rebuilt? |
| --- | --- | --- |
| `opa` | Served copies of the published release; `labels`, `feedback`, `ops` | Served copies: yes, from the lake. The rest: no |
| `opa_lake` | The lake's catalog | No |
| `opa_orchestrator` | The orchestrator's run and event history | No |

Schemas of the `opa` database:

| Schema | Content | Written by |
| --- | --- | --- |
| `silver` | Published window of silver tables | Publish |
| `inference` | Published inference tables of the release | Publish |
| `gold` | Dimensions, facts, and views of the release | Publish |
| `ref` | Reference data of the release | Publish |
| `meta` | Release information, accounting, evaluation, coverage | Publish |
| `labels` | Human judgments | Applications |
| `feedback` | User flags | Applications |
| `ops` | Operations log: deployments, backups, interventions | Operations |
| `sandbox_<user>` | Personal scratch space, not backed up | Its owner |

- **ARC-15 (MUST)** No user table lives in `public`. Extensions are the
  only objects there.
- **ARC-16 (MUST)** Bronze is not published to the serving database. It is
  queried in the lake.

## 7. Identifiers

- **ARC-20 (MUST)** Every entity has a deterministic identifier computed
  from its natural key, so that rebuilding the system from raw files
  yields the same identifiers.
- **ARC-21 (MUST)** The identifier of an entity is a 128-bit value:

  ```text
  id = md5( entity_name || U+001F || field_1 || U+001F || ... || field_n )
  ```

  rendered as a UUID. Fields are rendered as text: timestamps as UTC
  `YYYY-MM-DDTHH:MM:SS.ffffff`, dates as `YYYY-MM-DD`, integers without
  padding, absent values as the empty string. The separator is the unit
  separator character.
- **ARC-22 (MUST)** The function has one SQL implementation (a dbt macro)
  and one Python implementation, and a test proves they agree on a
  shared set of cases. No other code builds identifiers.
- **ARC-23 (MUST)** High-volume rows whose natural key is already compact
  use it directly and get no surrogate: a ping is identified by
  `(device_id, metric_timestamp, ping_seq)`, a stop event by
  `(run_id, stop_seq)`.

| Identifier | Natural key |
| --- | --- |
| `afc_trip_id` | company code, fleet number, line number, direction, trip open time, trip close time, service date |
| `boarding_id` | event identifier when present; otherwise trip identifier, tap time, card identifier, passenger type, amount paid, occurrence number |
| `pattern_id` | route, direction, ordered stop identifiers, shape geometry digest |
| `activity_id` | track owner, activity start time |
| `run_id` | track owner, run start time |
| `zone_id` | zone kind, zone name, company |
| `card_key` | keyed hash of the card identifier (`SEC-21`) |

- **ARC-24 (MUST)** Identifiers of inferred entities (`activity_id`,
  `run_id`) are stable across rebuilds whenever the inferred start time
  is unchanged. They are not a promise that two model versions agree.

### Canonical keys

- **ARC-25 (MUST)** Each business key has exactly one canonical form, used
  in silver and every later layer:

| Key | Canonical form |
| --- | --- |
| `bus_id` | Digits of the fleet number, left-padded with zeros to 5 characters; longer values are kept whole |
| `company_id` | First 2 characters of `bus_id` |
| `route_id` | Digits of the line number, left-padded with zeros to 4 characters; longer or non-numeric values are kept whole, trimmed and upper-cased |
| `direction` | `I` (outbound), `V` (return), or absent for a circular pattern |
| `device_id` | As delivered by the AVL source, trimmed |
| `stop_id` | As delivered by GTFS, trimmed |

- **ARC-26 (MUST)** Source values are kept next to canonical ones wherever
  canonicalization changes the text (for example `vehicle_number` and
  `bus_id`).

## 8. Time

- **ARC-30 (MUST)** Three notions of "day" are distinct columns and are
  never substituted for one another:

| Column | Definition |
| --- | --- |
| `event_date` | UTC date of the event timestamp |
| `local_date` | Calendar date of the event in `America/Fortaleza` |
| `operational_date` | Local date of the event shifted back by `time.operational_day_cutoff` (default 03:00), so that service after midnight belongs to the previous day |

- **ARC-31 (MUST)** All instants are stored as UTC timestamps with time
  zone. Local wall-clock values are derived, never stored as naive
  timestamps.
- **ARC-32 (MUST)** Conversions use the zone name `America/Fortaleza`.
- **ARC-33 (MUST)** The AFC `service_date` is kept exactly as recorded. It
  is the agency's attribution and is not redefined.
- **ARC-34 (MUST)** The unit of inference is the operational day. A worker
  processing operational day D reads events from D at the cutoff to D+1 at
  the cutoff, plus a margin of `time.day_margin_min` (default 30 minutes)
  on each side so that activities crossing the boundary are seen whole.
  Each activity is assigned to the operational day in which it starts.

The default cutoff is where tap volume is lowest (Profile: 02:00 to 03:59
local). Phase P4 confirms it against the full data.

## 9. Partitions, snapshots, and releases

- Every time-based table is **partitioned by month** in the orchestrator.
  A month of a table is the unit that is materialized, checked, and
  rematerialized. Inside the lake, large tables are physically
  partitioned by day (`PERF-1`).
- The **working state** of the lake is its latest snapshot. It changes
  whenever a partition is materialized.
- A **release** is a named snapshot of the lake that has passed every
  gate, together with the months it covers and the versions it pins. Its
  identifier has the form `YYYY.MM.n`.
- **Publishing** loads one release into the serving database from that
  snapshot. Exactly one release is published at a time.

- **ARC-40 (MUST)** Published data changes only by publishing a release.
  Work in progress in the lake is never visible in the serving database.
- **ARC-41 (MUST)** A release is evaluated and published from its
  snapshot, not from the working state, so later work cannot change what
  a release contains.
- **ARC-42 (MUST)** A snapshot referenced by a retained release is never
  expired, and its files are never removed.
- **ARC-43 (MUST)** A release records everything needed to reproduce it:
  the snapshot, the code version, the parameter digest, the reference
  data version, the model releases, and the state of the raw manifest.

Details are in [09-quality-and-lineage.md](09-quality-and-lineage.md).

## 10. Rules that hold everywhere

- **ARC-50 (MUST)** Determinism. A step run twice on the same inputs
  produces the same rows. Randomness is seeded from stable inputs.
  Results never depend on hash ordering, on the order files are listed,
  or on how work was split among workers.
- **ARC-51 (MUST)** Idempotence. Rematerializing a partition replaces that
  partition and nothing else. Reingesting a raw file replaces the rows
  of that file and nothing else.
- **ARC-52 (MUST)** Staleness is tracked by the orchestrator, not by hand.
  A partition is rematerialized when an upstream partition it depends on
  was updated, or when the code version, the parameters, the reference
  data, or a model release it uses has changed. Nothing else triggers
  work, and nothing stale is released (`DQ-26`).
- **ARC-53 (MUST)** Every asset has a code version. For dbt models it is
  derived from the model's SQL. For Python assets it is declared, and a
  test compares a digest of the component's source files with a recorded
  value, failing when the source changed and the version did not.
- **ARC-54 (MUST)** No step depends on state that is not a declared
  input: not the current date, the host name, the working directory, or
  leftovers of an earlier run.
- **ARC-55 (MUST)** Late-arriving data is normal. A newly ingested raw
  file that contributes rows to a month already built makes that month
  stale in every table downstream of it, and it is rebuilt.

## 11. Technology

| Concern | Choice | Alternatives considered |
| --- | --- | --- |
| Table format | DuckLake 1.x, Parquet data files, catalog in PostgreSQL | Plain partitioned Parquet (the fallback); Apache Iceberg and Delta Lake (the wider industry standards, heavier to operate on one host: Iceberg needs a catalog service, Delta has no lake-wide snapshot) |
| Query engine | DuckDB | Spark (oversized for one host) |
| Transformations | dbt Core with the DuckDB adapter | Hand-written SQL runner (rejected: reimplements dbt) |
| Orchestration | Dagster, open-source deployment | Command-line orchestration with timers (rejected: reimplements an orchestrator); Airflow (task-centered, weaker fit for partitioned tables and checks) |
| Dataframes | Polars | pandas (slower, more memory) |
| Serving database | PostgreSQL 18 with PostGIS 3.6 | Telemetry stored in PostgreSQL (rejected: storage and per-vehicle access cost) |
| Geometry in Python | Shapely 2, pyproj | Calling the database per row (rejected: speed) |
| Raw fetch | rclone | Provider-specific scripts |
| Backup | PostgreSQL dumps plus restic | Continuous archiving (optional later, `BKP-9`) |
| Serving schema migration | dbmate, plain SQL | Framework-specific migration tools |
| Applications | Streamlit | A custom web application |
| Containers | Docker Compose | Kubernetes (oversized) |

Exact versions are pinned in the repository (`ENG-10`, `PLT-21`).
