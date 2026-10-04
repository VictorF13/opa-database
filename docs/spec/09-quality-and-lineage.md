# 09. Quality and lineage

This document defines how the system proves that its data is complete and
correct, how every value is traced to its origin, and how releases are
made.

## 1. Where records are kept

| Record | Kept in | Why there |
| --- | --- | --- |
| Runs, materializations, check results, schedules | The orchestrator's history | It is the orchestrator's job, and its interface shows them |
| What the data is and where it came from | The lake's `meta` schema | It must travel with the data and be queryable with it |
| What people did to the system | `ops` in the serving database | It is written by operations, outside any run |

`meta` tables in the lake:

| Table | One row per | Purpose |
| --- | --- | --- |
| `raw_file`, `raw_file_member` | Raw file version; archive member | The manifest (`RAW-12`) |
| `ingest_file` | Raw file and bronze table | Record counts, quarantine counts, element counts (`BRZ-8`) |
| `schema_drift_event` | Unexpected field or structure | What changed in a source and when |
| `coverage_day` | Source and date | Whether data for the day is complete, partial, or missing, and why |
| `materialization` | Table and partition | The latest materialization: code version, reference version, model releases, time, row count, run |
| `accounting_day` | Invariant and date | The counts on both sides |
| `evaluation` | Snapshot, component, metric | Value, sample size |
| `benchmark` | Snapshot and operation | Timing against its budget |
| `release` | Release | Snapshot, months, pinned versions, gate results, status |
| `model_release` | Released model version | Artifact digest, label snapshot, metrics, status |

- **DQ-1 (MUST)** Every materialization writes its provenance to
  `meta.materialization` in the same transaction as its data
  (`ARC-11`), and reports the same facts to the orchestrator.
- **DQ-2 (MUST)** A release is self-describing. Everything needed to say
  what a release contains and how it was produced is in the lake at its
  snapshot. The orchestrator's history is a convenience, not a
  dependency.

## 2. Lineage

- **DQ-3 (MUST)** Row lineage: every silver row names its bronze row
  (`_file_id`, `_row`); every bronze row names its raw file. Every gold
  and inference row has a key that leads to the silver rows it was built
  from.
- **DQ-4 (MUST)** Table lineage is the orchestrator's asset graph and
  dbt's documentation, both generated from the code. No lineage diagram
  is maintained by hand.
- **DQ-5 (MUST)** `opa lineage <table> <key>` prints the chain from a
  published row back to raw file names and positions.

## 3. Accounting

Accounting is the proof that nothing was lost. Each invariant is a test
that runs with the tables it concerns, and its counts are stored per day
in `meta.accounting_day`.

- **DQ-10 (MUST)** These invariants hold exactly. A violation blocks
  everything downstream of the table that failed, and nothing that fails
  one can be released.

| ID | Invariant |
| --- | --- |
| A1 | Records in each raw file = bronze rows + quarantined records |
| A2 | Bronze rows of an event date = silver rows + rejected rows + duplicate rows |
| A3 | Rows of `silver.afc_trips` = rows of `fact_afc_trip`, per service date |
| A4 | Rows of `silver.afc_boardings` = rows of `fact_boarding`, per service date, and the sums of amounts paid are equal |
| A5 | Every trip record has exactly one reconciliation status and one service class |
| A6 | Every tap has a fare class and an assignment reason |
| A7 | Pings of each device-day in silver = rows of `ping_activity`, each with one activity or `noise` |
| A8 | Activities of a device do not overlap and, with gaps, cover the span from its first to its last ping |
| A9 | Runs of a device do not overlap; links do not overlap per bus or per device |
| A10 | Stop events of a run = stops of the run's pattern |
| A11 | Every bus with a trip record or tap has a `fact_bus_day` row and every device with a ping a `fact_device_day` row, and their counts sum to the fact totals |
| A12 | A tap assigned to a run belongs to the bus linked to the run's device, and its time is inside the run or its documented layover window |
| A13 | Every reference between tables resolves |

- **DQ-11 (MUST)** Every summary table or view reconciles with the facts
  it summarizes: its totals equal the totals computed directly.

## 4. Checks

| Kind | Examples | On failure |
| --- | --- | --- |
| Contract | Column names, types, nullability | Blocks |
| Accounting | Section 3 | Blocks |
| Integrity | Uniqueness, references, non-overlap, time order, accepted values | Blocks |
| Plausibility | Daily volumes, null rates, shares by category | Warns or blocks by threshold |
| Freshness | Age of the newest raw file per source | Warns |

- **DQ-15 (MUST)** Checks use the tools' own mechanisms: dbt data tests
  and model contracts for every lake table, including tables written by
  Python, which are declared as dbt sources; dbt unit tests for
  transformation logic; and the orchestrator's asset checks for
  anything that needs Python. Each check has a name, a severity, and an
  owner table. A blocking check that fails stops every downstream
  materialization.
- **DQ-12 (MUST)** Plausibility checks compare each day with a reference
  (the trailing median of the same day type) and have two thresholds:
  outside the warning band the check warns; outside the failure band it
  blocks. The thresholds are parameters (`dq.*`). Initial bands for daily
  volume of pings, taps, and trip records: warn outside 0.5 to 1.5 times
  the reference, block outside 0.2 to 3 times.
- **DQ-13 (MUST)** A day whose source data is known to be incomplete
  (an empty or missing raw file, a truncated dump) is recorded in
  `meta.coverage_day` with the evidence. Analysts can tell an absent
  service from absent data.
- **DQ-14 (MUST)** A known, explained anomaly (a strike, a holiday, a
  documented outage) is acknowledged in `ref.acknowledged_anomaly` with
  the check, the dates, and the reason. An acknowledged anomaly does not
  block. Acknowledgements are reviewed like any reference change.
- **DQ-16 (MUST)** The minimum plausibility set is: rows per day per
  table; share of null in each nullable column; share of pings flagged;
  share of taps geotagged; share of fleet-days linked; share of passenger
  taps assigned to a run; share of stop events observed; count per
  reconciliation status. Each is stored per day and compared across
  releases.

## 5. Materializations and snapshots

- **DQ-20 (MUST)** The unit of materialization is one month of one table
  (`ARC-52`). A month is complete only when its blocking checks pass.
- **DQ-21 (MUST)** The latest snapshot of the lake is a working state. It
  may contain months that are being rebuilt or that failed a check.
  Nobody is served from it (`ARC-40`).
- **DQ-22 (MUST)** A snapshot that no retained release refers to is
  expired after `dq.snapshot_retention_days` (default 30), and the files
  only it used are then removed. A snapshot that a retained release
  refers to is never expired (`ARC-42`).
- **DQ-23 (MUST)** Lake maintenance (snapshot expiry, removal of
  unreferenced files, compaction of small files) runs on a schedule, as
  heavy work (`PLT-34`), and never during a backup.

## 6. Releases

- **DQ-25 (MUST)** A release is created by naming the months it covers.
  Creation records the current snapshot of the lake and everything
  `ARC-43` lists. Its identifier is `YYYY.MM.n`.
- **DQ-26 (MUST)** A release is published only if it passes the release
  gate, evaluated at its snapshot:
  1. every table and month in scope is fresh: nothing upstream of it
     changed, and no code version, parameter, reference data, or model
     release it uses changed, since it was materialized;
  2. every blocking check and every accounting invariant passes;
  3. every inference gate of `INF-95` passes;
  4. the benchmark meets the performance budgets (`PERF-42`);
  5. every drift report is acknowledged (`DQ-30`).

  Gate results are stored with the release.
- **DQ-27 (MUST)** Each release has release notes generated from the
  lake: what changed since the previous release (months rebuilt, models,
  parameters, reference data, accepted proposals), metric changes, and
  known limitations.
- **DQ-28 (MUST)** The last `dq.release_retention` releases (default 3)
  are retained. A release can be **pinned**; a pinned release is retained
  until unpinned. A release cited in a publication is pinned.
- **DQ-29 (MUST)** Publishing replaces the serving database's content
  with the release, month by month, each swap atomic for readers
  (`PERF-31`), and updates the published-release record last. A failed
  publish leaves the previous release in place.

## 7. Drift monitoring

- **DQ-30 (MUST)** For every release, each tracked metric (`DQ-16`,
  `INF-8`, `INF-97`) is compared with the previous release and with its
  own history. A change larger than the metric's tolerance is reported
  in the release notes and requires acknowledgement before publication.
- **DQ-31 (SHOULD)** Metrics are also tracked along time within a
  release (month over month), so that a change in the source data
  itself, such as a new device family or a new fare code, is noticed.

## 8. Quarantine and rejects

- **DQ-35 (MUST)** Quarantined records and rejected rows are data. They
  are retained, counted in accounting, and summarized per reason for
  every ingest.
- **DQ-36 (MUST)** A reason that exceeds its expected share (`dq.*`)
  raises a warning. A new reason, never seen before, raises a notice.

## 9. Acceptance

Quality and lineage are accepted when: every invariant in `DQ-10` is
implemented with a test case that makes it fail on purpose; `opa lineage`
resolves a sample of rows from every gold fact back to raw files; a
release's notes can be regenerated from the lake alone; and a release
created before later work in the lake still evaluates and publishes
identically afterwards.
