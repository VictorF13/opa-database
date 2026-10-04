# 02. Architecture

## 1. System context

```text
  Remote raw store (read-only)
            |
            |  fetch, verify checksum
            v
+--------------------------- BULK tier: the lake ---------------------------+
|                                                                           |
|  raw/  -->  bronze/  -->  silver/  -->  inference  -->  gold              |
|  files      lossless      typed,        observed        analysis-ready    |
|  as         Parquet,      cleaned,      timelines,      star schema       |
|  received   with lineage  conformed     links, runs                       |
|                                                                           |
|  reference data (versioned in the repository) feeds silver and above      |
+---------------------------------------------------------------------------+
            |
            |  publish a release: bulk load, constraints, indexes, grants
            v
+----------------------- FAST tier: PostgreSQL + PostGIS -------------------+
|  silver | inference | gold | ref | labels | feedback | meta               |
+---------------------------------------------------------------------------+
        ^                    ^                         ^
        |                    |                         |
    analysts            applications              pipeline state
    (SQL clients)       (labeling, explorer)      (builds, checks)
```

The lake holds the data. PostgreSQL serves it and holds the small amount of
state that people and the pipeline write directly (labels, feedback,
metadata).

## 2. Layers

| Layer | Job | Input | Shape | Never does |
| --- | --- | --- | --- | --- |
| Raw | Keep the original files exactly as received | Remote raw store | Original files plus a manifest | Modify or delete a file |
| Bronze | A lossless copy of raw in one uniform format, with lineage | Raw | One Parquet file per raw file, every field as text | Clean, type, drop, or merge |
| Silver | Typed, cleaned, deduplicated, conformed tables, one per source entity | Bronze, reference | Parquet partitioned by event date | Join sources or infer facts |
| Inference | Facts that no source states: links, timelines, patterns, reconciliation | Silver, reference, labels, released models | Parquet per build | Overwrite a source value |
| Gold | The analysis-ready model | Silver, inference, reference | Dimensions and facts per release | Compute new inference |

- **ARC-1 (MUST)** Each layer has exactly the job in the table. Work that
  belongs to a later layer is not done earlier, and the reverse.
- **ARC-2 (MUST)** A layer reads only from layers to its left, from
  reference data, and, for inference, from labels and released models. No
  layer reads from the serving database to compute data, with the single
  exception of labels and feedback, which originate there (`ARC-5`).
- **ARC-3 (MUST)** Inference is part of the silver tier in the
  conventional three-tier reading of this architecture: it is cleaned,
  conformed, entity-level data. It is named separately because its rows
  are estimates with a confidence, not transcriptions of a source.

## 3. Storage roles

- **ARC-4 (MUST)** The lake is the system of record for all data in the
  raw, bronze, silver, inference, and gold layers.
- **ARC-5 (MUST)** The serving database is the system of record only for
  state written by people or by the running pipeline: `labels`,
  `feedback`, and `meta`. That state is exported to the lake on a schedule
  (`BKP-4`) so the lake plus the repository are sufficient to rebuild the
  whole system.
- **ARC-6 (MUST)** Every table in the serving database other than those in
  `ARC-5` can be dropped and reloaded from the lake by a single command
  (`OPS-6`) with no loss.
- **ARC-7 (MUST)** Computation that transforms data runs outside the
  serving database, on Parquet files. The serving database runs queries
  from analysts and applications, and the bulk loads that publish a
  release.

Rationale: telemetry is append-only and columnar by nature. As Parquet it
is roughly ten times smaller than as database rows with indexes, a single
vehicle-day is one contiguous read, and the work parallelizes cleanly
across cores. Keeping the database disposable also removes the most
dangerous class of operator mistake.

## 4. Compute

| Engine | Used for |
| --- | --- |
| Polars | Parsing, typing, and cleaning (bronze and silver); columnar transforms |
| DuckDB | Set-based SQL over Parquet: joins, as-of joins, aggregation, gold assembly, ad hoc analysis |
| Python with NumPy, Shapely, pyproj, and Numba | Sequential per-vehicle algorithms: track segmentation, map matching, stop events |
| PostgreSQL with PostGIS | Serving queries; constraints; application state |

- **ARC-8 (MUST)** The unit of parallel work is an entity-day (one device
  or one bus on one operational day). Units are independent and are
  processed by a pool of worker processes (`PERF-8`).
- **ARC-9 (SHOULD)** A learned model is introduced only where a
  transparent rule or scoring method measurably underperforms on the
  frozen test set (`INF-6`).

## 5. Lake layout

```text
${OPA_BULK_ROOT}/
  raw/
    files/<source>/<original relative path>       immutable mirror
    versions/<file_id>/<file name>                superseded contents
  lake/
    bronze/<dataset>/period=<YYYY-MM>/<file_id>.parquet
    bronze/_quarantine/<dataset>/<file_id>.parquet
    silver/<table>/<partition column>=<value>/data.parquet
    silver/_rejects/<table>/<partition column>=<value>/data.parquet
    builds/<build_id>/inference/<table>/month=<YYYY-MM>/part-<n>.parquet
    builds/<build_id>/gold/<table>/month=<YYYY-MM>/part-<n>.parquet
    releases/<release_id>/dims/<table>/data.parquet
    exports/<kind>/<timestamp>/...                snapshots of app state
  models/<component>/<version>/                   released model artifacts
```

- **ARC-10 (MUST)** Files are Parquet with Zstandard compression. Layout,
  sort order, and file sizing follow `PERF-1` to `PERF-4`.
- **ARC-11 (MUST)** Every Parquet file carries, in its key-value footer
  metadata, the identifier of the task run that wrote it, the code
  version, the transform version, and the fingerprint of its inputs. The
  metadata catalog (`meta`) can be rebuilt from the files alone.
- **ARC-12 (MUST)** Writes are atomic. A writer produces a temporary file
  in the destination directory and renames it into place. A reader never
  sees a partial file.
- **ARC-13 (MUST)** At most one writer holds a given partition at a time,
  enforced by an advisory lock recorded in `meta`.
- **ARC-14 (MUST)** Bronze and silver hold one current version of each
  partition. Inference and gold are versioned by build (section 9).

A table format with snapshots (Delta Lake, Apache Iceberg, DuckLake) was
considered and deferred: plain partitioned Parquet with a manifest in
`meta` is sufficient for a single writer on one host and keeps every file
readable by every tool. The decision is revisited if concurrent writers or
time travel beyond releases become requirements.

## 6. Serving database schemas

| Schema | Content | Written by |
| --- | --- | --- |
| `silver` | Published window of silver tables | Publish |
| `inference` | Published inference tables of the current release | Publish |
| `gold` | Dimensions, facts, and summary tables of the current release | Publish |
| `ref` | Reference data, loaded from the repository | Publish |
| `labels` | Human judgments | Applications |
| `feedback` | User flags | Applications |
| `meta` | Manifest, builds, releases, checks, operations log | Pipeline |
| `sandbox_<user>` | Personal scratch space, not backed up | Its owner |

- **ARC-15 (MUST)** No user table lives in `public`. Extensions are the
  only objects there.
- **ARC-16 (MUST)** Bronze is not published to the serving database. It is
  queried in the lake with DuckDB.

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
- **ARC-22 (MUST)** The function has one Python implementation and one SQL
  implementation, and a test proves they agree on a shared set of cases.
  No other code builds identifiers.
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
  `run_id`) are stable across builds whenever the inferred start time is
  unchanged. They are not a promise that two model versions agree.

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
local). P0 confirms it against the full data.

## 9. Builds and releases

Bronze and silver are pure functions of raw files, reference data, and
code. They exist once. Inference and gold depend on models and evolve, so
they are versioned.

- A **build** is one execution of the inference and gold steps for one
  month. It has a `build_id` and writes under `lake/builds/<build_id>/`.
  A build is immutable once finished.
- A **release** is a named, immutable selection of exactly one finished
  build per month, plus the dimension tables assembled across those
  months. It has a `release_id` of the form `YYYY.MM.n`.
- **Publishing** loads one release into the serving database. Exactly one
  release is published at a time.

- **ARC-40 (MUST)** Published data changes only by publishing a release.
- **ARC-41 (MUST)** A release reuses the builds of months that did not
  change. It never copies them.
- **ARC-42 (MUST)** A build referenced by a retained release is never
  modified or removed.
- **ARC-43 (MUST)** A release records everything needed to reproduce it:
  code version, parameter digest, reference data version, model releases,
  and the raw manifest state.

Details are in [09-quality-and-lineage.md](09-quality-and-lineage.md).

## 10. Rules that hold everywhere

- **ARC-50 (MUST)** Determinism. A step run twice on the same inputs
  produces byte-identical row content in the same order. Randomness is
  seeded from stable inputs. Iteration order never depends on hash
  ordering or on the filesystem.
- **ARC-51 (MUST)** Idempotence. Re-running a step replaces its own output
  and nothing else. The output location of a step is a function of its
  inputs' identity, never of processing time or of the content of other
  inputs.
- **ARC-52 (MUST)** Staleness is computed. Each output records a
  fingerprint of its inputs (content digests, transform version, parameter
  digest, reference version). A step runs when the fingerprint differs and
  is skipped when it matches.
- **ARC-53 (MUST)** Each transform declares an integer version, bumped
  whenever its logic changes. A test compares a digest of the transform's
  source files with a recorded value and fails if the source changed and
  the version did not.
- **ARC-54 (MUST)** No step depends on state that is not an input: no
  reliance on the current date, the host name, the working directory, or
  the leftover contents of a scratch table.
- **ARC-55 (MUST)** Late-arriving data is normal. When a new raw file
  contributes rows to an event date that was already built, that date and
  everything downstream of it become stale and are rebuilt.

## 11. Technology

| Concern | Choice | Alternatives considered |
| --- | --- | --- |
| File format | Parquet, Zstandard | Table formats with snapshots (deferred, section 5) |
| Dataframe engine | Polars | pandas (slower, more memory) |
| SQL over files | DuckDB | Spark (oversized for one host) |
| Serving database | PostgreSQL 18 with PostGIS 3.6 | Telemetry in PostgreSQL with compression extensions (rejected: storage and per-vehicle access cost) |
| Geometry in Python | Shapely 2, pyproj | Calling the database per row (rejected: speed) |
| Validation | Pandera schemas on Polars frames | Hand-written checks |
| Transformations | Plain SQL files and Python modules | A transformation framework (not adopted, `D-15`) |
| Orchestration | Command line interface with computed staleness, systemd timers | An orchestrator service (deferred, `D-15`) |
| Raw fetch | rclone | Provider-specific scripts |
| Backup | PostgreSQL dumps plus restic | Continuous archiving (optional later, `BKP-9`) |
| Schema migration | dbmate, plain SQL | Framework-specific migration tools |
| Applications | Streamlit | A custom web application |
| Containers | Docker Compose | Kubernetes (oversized) |

Exact versions are pinned in the repository (`ENG-10`, `PLT-21`).
