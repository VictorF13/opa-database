# 17. Roadmap

The system is built as a sequence of phases. Each phase delivers something
that works and can be verified on its own, and each ends with explicit
acceptance criteria. No phase starts before the previous one is accepted,
except where the table of dependencies says phases can overlap.

## 1. Rules for every phase

A phase is done when:

1. Its code is merged through pull requests that pass every check
   (`DLV-31`).
2. Its requirements have tests, and the end-to-end test on the synthetic
   world (`ENG-36`) covers everything built so far.
3. Its runbooks and reference documentation exist and were followed once
   by someone other than the author, or by the author on a scratch
   environment.
4. This specification matches what was built. Anything learned that
   changes the design is recorded as a decision record and reflected
   here.
5. Its acceptance criteria below are demonstrated and the evidence is
   linked from the phase's milestone.

## 2. Phases

| Phase | Name | Size | Depends on |
| --- | --- | --- | --- |
| P0 | Repository and delivery foundation | M | |
| P1 | Platform | M | P0 |
| P2 | Raw store and inventory | S | P1 |
| P3 | Bronze | M | P2 |
| P4 | Silver and reference data | L | P3 |
| P5 | Tracks, patterns, and segmentation | XL | P4 |
| P6 | Bus and device linkage | L | P5 |
| P7 | Reconciliation, boardings, stop events | XL | P6 |
| P8 | Gold, quality, first release | L | P7 |
| P9 | Applications and the improvement loop | L | P8 (a minimal labeling application is part of P5) |
| P10 | Scale-out | L | P8 |

Sizes are relative (S, M, L, XL), not dates.

### P0. Repository and delivery foundation

**Goal:** a repository in which every later change is automatically held
to the standard.

Deliverables: the uv workspace and four package skeletons; Ruff, ty,
pytest, and prek configured as specified; the continuous integration
workflow; branch rulesets and repository settings; pull request and issue
templates; community files; release automation; the documentation
skeleton with the first decision records; the skeleton of the synthetic
world generator; `CLAUDE.md`.

Covers: `ENG-1` to `ENG-59`, `DLV-1` to `DLV-61` except deployment.

Acceptance:

- The acceptance commands of [13-engineering.md](13-engineering.md) pass
  on a clean clone.
- Acceptance items 1 to 5 of [14-delivery.md](14-delivery.md) are
  demonstrated with a trivial change.

### P1. Platform

**Goal:** a database instance and storage layout that are fast, safe, and
defined entirely in the repository.

Deliverables: tier configuration and preflight; the compose file and
PostgreSQL configuration; migrations for `meta`, `ref`, `labels`,
`feedback`, and schemas; roles, grants, and access verification; backups,
restore test, and health check with timers; `opa ops` commands; the
evaluation of `pg_duckdb` (`PLT-24`); the platform runbooks.

Covers: `PLT-*`, `SEC-1` to `SEC-13`, `SEC-30` to `SEC-52`, `BKP-*`,
`OPS-30` to `OPS-35`, `COX-*`.

Acceptance:

- Acceptance of [03-platform.md](03-platform.md).
- Acceptance items 1 and 3 of
  [12-backup-and-recovery.md](12-backup-and-recovery.md).
- Acceptance items 1 to 4 of
  [11-access-and-security.md](11-access-and-security.md).
- `opa ops status` reports each production-readiness criterion.

### P2. Raw store and inventory

**Goal:** know exactly what raw data exists, and hold a verified mirror
of what the reference month needs.

Deliverables: `opa raw inventory`, `fetch`, `verify`; the manifest; path
rules with tests; the raw inventory report (years per source, sizes,
layouts, anomalies) in `docs/reference/`.

Covers: `RAW-*`, `REF-12`.

Acceptance:

- Every object in the remote store is classified (`RAW-12`).
- The reference month and its neighbors are mirrored and verified.
- The inventory report is reviewed, and
  [01-source-data.md](01-source-data.md) is corrected where it differs.
- A capacity projection for the full history exists (`PLT-51`).

### P3. Bronze

**Goal:** a lossless, queryable copy of every raw file of the reference
month and its neighbors.

Deliverables: every bronze dataset; quarantine; coverage index; drift
events; `opa bronze` commands.

Covers: `BRZ-*`, `DQ-1` to `DQ-3`, accounting invariant A1.

Acceptance: acceptance of [04-raw-and-bronze.md](04-raw-and-bronze.md).

### P4. Silver and reference data

**Goal:** typed, clean, conformed tables and the curated reference data
they need, with every profile fact re-measured.

Deliverables: all silver tables and contracts; rejects; quality flags;
reference tables, vocabularies, and the parameter registry; sentinel and
code tables from profiling; the profile report that confirms or corrects
each **Profile** statement of this specification.

Covers: `SLV-*`, `REF-*`, accounting invariant A2, `DQ-12` to `DQ-16`.

Acceptance:

- Acceptance of [05-silver.md](05-silver.md) and
  [06-reference-data.md](06-reference-data.md).
- Every profile fact is confirmed or corrected in the specification, and
  every parameter marked "P0" or "P4" has its measured basis.

### P5. Tracks, patterns, and segmentation

**Goal:** a complete, categorized timeline for every device, and a
correct set of schedule patterns.

Deliverables: track preparation; device profiles; zone validation
report; schedule patterns; activity segmentation, runs, and blocks; a
minimal labeling application with the timeline annotation and link
review queues; the first annotated bus-days; segmentation evaluation.

Covers: `INF-1` to `INF-29`, `APP-1` to `APP-6`, `APP-12` (two queues),
`APP-20` to `APP-26`, accounting invariants A7 to A9.

Acceptance:

- Invariants A7, A8, and A9 hold for the reference month.
- At least 30 annotated bus-days exist with the required stratification
  (`INF-92`).
- The segmentation gates of `INF-95` are met on the frozen bus-days.
- Segmented routes (circular routes published as segments) produce
  chained runs with unique stop positions.

### P6. Bus and device linkage

**Goal:** every bus-day linked to a device, or explained.

Deliverables: candidate generation; every evidence family; scoring,
daily assignment, and consolidation; unlinked reports; tap-derived
tracks; linkage evaluation.

Covers: `INF-30` to `INF-39`, `INF-50` to `INF-52`.

Acceptance:

- The linkage gates of `INF-95` are met.
- Every bus with a trip record or tap in the reference month is linked
  or has an unlinked reason; every mobile device is linked or has one.
- Results are reported separately for buses with and without geotagged
  taps.
- Any link can be explained from `link_evidence_day`.

### P7. Reconciliation, boardings, stop events

**Goal:** every trip record reconciled, every tap placed, every stop of
every run timed or explained.

Deliverables: reconciliation with statuses, classes, flags, and
corrections; boarding assignment, positions, and boarding stops; stop
events; learned pattern proposals; rule exception proposals; incident
candidates; fare code profile; both passes of a build.

Covers: `INF-40` to `INF-42`, `INF-60` to `INF-99`, accounting
invariants A5, A6, A10, A12.

Acceptance:

- The reconciliation and stop event gates of `INF-95` are met.
- Invariants A5, A6, A10, and A12 hold for the reference month.
- The known direction exception (route 614) is rediscovered from the
  data by `INF-73`, without being told.
- Fare code proposals have been reviewed and `ref.afc_code` updated.

### P8. Gold, quality, and the first release

**Goal:** milestone M1. The reference month is published as the first
release.

Deliverables: gold dimensions, facts, summaries, and views; all
accounting invariants and checks; lineage command; builds, releases,
gates, release notes; publishing with swap and access verification;
benchmark suite; data dictionary; query guide.

Covers: `GLD-*`, `DQ-*`, `PERF-*`, `OPS-1` to `OPS-14`.

Acceptance (M1):

- Acceptance of [08-gold.md](08-gold.md),
  [09-quality-and-lineage.md](09-quality-and-lineage.md), and
  [15-operations.md](15-operations.md).
- Every invariant of `DQ-10` holds for the reference month.
- Every gate of `INF-95` passes; the release is created, gated, and
  published by command.
- `opa bench` meets the budgets of `PERF-40`, or the budgets are revised
  with measurements.
- The whole month is rebuilt from raw files on a scratch lake and
  produces identical content.

### P9. Applications and the improvement loop

**Goal:** people can explore, flag, and label, and their input improves
the next release through gates.

Deliverables: the explorer; all labeling queues; flag triage; label
snapshots; scheduled training and tuning; model releases; loop health
metrics.

Covers: `APP-*`.

Acceptance: acceptance of
[16-apps-and-feedback.md](16-apps-and-feedback.md), and one full cycle
completed: a flag leads to a label, to a retrained or retuned component,
to a gated model release, to a new data release.

### P10. Scale-out

**Goal:** milestones M2 and M3.

Deliverables: all of 2023 built and released (M2); every year present in
the raw store built and released, including any earlier raw formats
found by the inventory (M3); published window and capacity settled;
budgets confirmed at scale.

Acceptance:

- M2: every month of 2023 passes all invariants and gates; the year
  builds within the budget of `PERF-40`.
- M3: the same for every available year. Periods where a source is
  missing are recorded in `meta.coverage_day`, not skipped silently.
- Drift monitoring (`DQ-30`) has been reviewed across the full history.

## 3. Production readiness

Independent of the phases, the system is declared production-ready when
M1 is met and every criterion of `BKP-12` holds. Until then
`opa ops status` says which criteria are missing.

## 4. Order of risk

The riskiest assumptions are tested first:

| Assumption | Tested in | If false |
| --- | --- | --- |
| The remote store holds complete raw data for the reference month | P2 | Choose another reference month |
| Tap coordinates identify the device for most of the fleet | P4 (profile), P6 | Linkage relies more on the other evidence families |
| Segmentation meets its gates with schedule patterns alone | P5 | Bring learned patterns forward from P7 |
| The reference host meets the budgets | P1, P8 | Revise budgets or the published window |
| `pg_duckdb` works on the chosen image | P1 | Use the DuckDB catalog (`OPS-14`) |
