# 09. Quality and lineage

This document defines how the system proves that its data is complete and
correct, how every value is traced to its origin, and how builds and
releases are recorded.

## 1. Metadata catalog

The `meta` schema is the pipeline's memory. It is written only by the
pipeline.

| Table | One row per | Purpose |
| --- | --- | --- |
| `raw_file`, `raw_file_member` | Raw file version; archive member | The manifest (`RAW-12`) |
| `bronze_file` | Raw file and dataset | Record counts, quarantine counts, element counts (`BRZ-8`) |
| `bronze_coverage` | File, dataset, event date | Which files feed which date (`BRZ-9`) |
| `silver_partition` | Table and partition | Row dispositions, input fingerprint (`SLV-6`) |
| `schema_drift_event` | Unexpected field or structure | What changed in a source and when |
| `coverage_day` | Source and date | Whether data for the day is complete, partial, or missing, and why |
| `task_run` | Execution of one task | Command, scope, times, status, code and transform versions, parameter and reference digests, input fingerprint, row counts |
| `build` | Inference and gold build of one month | Versions pinned, status, metrics |
| `release`, `release_member` | Release; month within a release | Which build each month uses |
| `published_release` | Single row | What the serving database currently holds |
| `model_release` | Released model version | Artifact digest, training label snapshot, metrics, status |
| `check_result` | Check execution | Scope, severity, outcome, observed and expected values |
| `accounting_day` | Invariant and date | The counts on both sides |
| `evaluation` | Build and metric | Value, sample size, comparison with the previous release |
| `ops_event`, `backup_run`, `deployment`, `lock` | Operational events | Operations log |

- **DQ-1 (MUST)** Every task execution writes a `task_run` row, whether it
  succeeds or fails.
- **DQ-2 (MUST)** The catalog can be reconstructed from the lake's file
  metadata (`ARC-11`) by `opa meta rebuild`. Losing the catalog loses no
  data and no lineage.

## 2. Lineage

- **DQ-3 (MUST)** Row lineage: every silver row names its bronze row
  (`_file_id`, `_row`); every bronze row names its raw file. Every gold
  and inference row has a key that leads to the silver rows it was built
  from.
- **DQ-4 (MUST)** Build lineage: every build records the code version,
  transform versions, parameter digest, reference version, model
  releases, and the manifest state it read. Given a release, the exact
  inputs can be listed.
- **DQ-5 (MUST)** `opa lineage <table> <key>` prints the chain from a
  published row back to raw file names and positions.

## 3. Accounting

Accounting is the proof that nothing was lost. Each invariant is checked
per day, on every build, and its counts are stored in
`meta.accounting_day`.

- **DQ-10 (MUST)** These invariants hold exactly. A violation fails the
  build or the publish that detects it.

| ID | Invariant |
| --- | --- |
| A1 | Records in each raw file = bronze rows + quarantined records |
| A2 | Bronze rows of a partition's inputs = silver rows + rejected rows + duplicate rows |
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
| A13 | Every foreign key resolves |

- **DQ-11 (MUST)** Every summary table reconciles with the facts it
  summarizes: its totals equal the totals computed directly.

## 4. Checks

| Kind | Examples | On failure |
| --- | --- | --- |
| Contract | Types, nullability, ranges, key uniqueness | Fail |
| Accounting | Section 3 | Fail |
| Integrity | Foreign keys, non-overlap, time order | Fail |
| Plausibility | Daily volumes, null rates, shares by category | Warn or fail by threshold |
| Freshness | Age of the newest raw file per source | Warn |

- **DQ-12 (MUST)** Plausibility checks compare each day with a reference
  (the trailing median of the same day type) and have two thresholds:
  outside the warning band the check warns; outside the failure band it
  fails. The thresholds are parameters (`dq.*`). Initial bands for daily
  volume of pings, taps, and trip records: warn outside 0.5 to 1.5 times
  the reference, fail outside 0.2 to 3 times.
- **DQ-13 (MUST)** A day whose source data is known to be incomplete
  (an empty or missing raw file, a truncated dump) is recorded in
  `meta.coverage_day` with the evidence. Analysts can tell an absent
  service from absent data.
- **DQ-14 (MUST)** A known, explained anomaly (a strike, a holiday, a
  documented outage) is acknowledged in `ref/acknowledged_anomalies` with
  the check, the dates, and the reason. An acknowledged anomaly does not
  fail a build. Acknowledgements are reviewed like any reference change.
- **DQ-15 (MUST)** Checks are code: each has an identifier, a severity, a
  query or function, and a test. Results go to `meta.check_result`.
- **DQ-16 (MUST)** The minimum plausibility set is: rows per day per
  table; share of null in each nullable column; share of pings flagged;
  share of taps geotagged; share of fleet-days linked; share of passenger
  taps assigned to a run; share of stop events observed; count per
  reconciliation status. Each is tracked per day and per release.

## 5. Builds

- **DQ-20 (MUST)** A build covers one month and has a status: `running`,
  `finished`, or `failed`. Only finished builds can join a release.
- **DQ-21 (MUST)** A finished build is immutable (`ARC-42`). To change a
  month, a new build is made.
- **DQ-22 (MUST)** A build runs all contract, accounting, and integrity
  checks and `opa infer eval` before it is marked finished.
- **DQ-23 (MUST)** Builds not referenced by any retained release are
  removed after `dq.build_retention_days` (default 30).

## 6. Releases

- **DQ-25 (MUST)** A release is created from finished builds, one per
  month, plus the dimension tables assembled across those months. Its
  identifier is `YYYY.MM.n`.
- **DQ-26 (MUST)** A release passes the gates of `INF-95` and all checks
  before it can be published. Gate results are stored with the release.
- **DQ-27 (MUST)** Each release has release notes generated from the
  catalog: what changed (months rebuilt, models, parameters, reference
  data, accepted proposals), metric changes against the previous
  release, and known limitations.
- **DQ-28 (MUST)** The last `dq.release_retention` releases (default 3)
  are retained in the lake. A release can be **pinned**; a pinned
  release is retained until unpinned. A release cited in a publication
  is pinned.
- **DQ-29 (MUST)** Publishing replaces the serving database's content
  with the release atomically from the reader's point of view, month by
  month (`PERF-31`), and updates `meta.published_release` last. A failed
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
  are retained, counted in accounting, and summarized per reason in a
  report for every ingest.
- **DQ-36 (MUST)** A reason that exceeds its expected share (`dq.*`)
  raises a warning. A new reason, never seen before, raises a notice.

## 9. Acceptance

Quality and lineage are accepted when: every invariant in `DQ-10` is
implemented with a test that makes it fail on purpose; `opa lineage`
resolves a sample of rows from every gold fact back to raw files; and a
release's notes can be regenerated from the catalog alone.
