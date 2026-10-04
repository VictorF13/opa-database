# 00. Overview

## 1. Purpose

OPA Database turns the raw operational data of Fortaleza's bus system into
a research database that people can trust. It ingests fare collection
records, vehicle GPS positions, schedules, and reference lists, and
produces a clean model of what every bus actually did: where it was, what
route it was serving, when it reached each stop, and who boarded where.

## 2. The problem

The city's transit data comes as three feeds that were never designed to
be joined.

| Feed | What it knows | What it does not know |
| --- | --- | --- |
| Fare collection (AFC) | The bus fleet number, the route and direction the driver selected, a "trip" the driver opened and closed, every fare tap | Where the bus really was, whether the trip record is accurate |
| Vehicle location (AVL) | A device identifier and its position every few seconds, with speed and heading | Which bus the device is installed in, what the bus was doing |
| Schedule (GTFS) | The planned routes, shapes, stops, and timetables | What was actually operated |

Research questions such as "when did the bus reach each stop" or "where do
people board" need facts that none of the feeds states:

1. **Which device is on which bus.** The published vehicle dictionaries are
   stale and contradict each other.
2. **What each bus was really doing.** A driver-operated trip record can be
   opened in the garage, left open across several runs, closed late, carry
   the wrong route or direction, or be missing entirely.
3. **What the network really looks like.** Schedules contain routes split
   into segments, unpublished short turns, and shapes that do not match the
   street.

The system exists to infer those facts, state how confident it is in each,
and never lose a single record while doing so.

## 3. Goals

| ID | Goal | Meaning |
| --- | --- | --- |
| G1 | Trustworthy | Every value can be traced to a raw file, a rule, and a model version. Known data problems are categorized, not hidden. |
| G2 | Complete | Every fare, every operator trip record, and every GPS ping is accounted for with a category and a reason. Nothing disappears silently. |
| G3 | Reproducible | The whole database can be rebuilt from raw files, reference data, labels, and code. Published data is versioned. |
| G4 | Resilient | Data survives a disk failure, an operator mistake, and a bad build. Recovery is tested, not assumed. |
| G5 | Fast | Queries and builds use the host's full capacity. The joins the research needs are designed for, not discovered later. |
| G6 | Scalable | The design holds for every year of data the agency has, not one month. |
| G7 | Self-improving | Human corrections flow back into the models through a gated loop, indefinitely. |
| G8 | Maintainable | Standard structure, strict tooling, tests, and documentation, usable by people and by coding agents. |
| G9 | Private | Personal data is minimized and access to it is deliberate. |

## 4. Non-goals

- Real-time ingestion or live vehicle tracking. The system is batch.
- Alighting (destination) inference. Boarding stops are in scope; where
  riders get off is a later extension that this design leaves room for.
- A public data portal or public API.
- High availability, replication, or multi-host deployment.
- Generalization to other cities. Naming and parameters are specific to
  Fortaleza, though nothing prevents later reuse.
- Hosting in a public cloud.

## 5. Principles

| ID | Principle |
| --- | --- |
| PR-1 | **Raw files are sacred.** They are never modified, and every derived row can be traced back to one. |
| PR-2 | **Bronze is lossless.** Nothing is cleaned, dropped, or reinterpreted before silver. |
| PR-3 | **Categorize and correct, never delete.** A wrong or odd record is kept, labeled, and, where possible, accompanied by a corrected value and the reason for it. |
| PR-4 | **Observation beats declaration.** What the GPS track shows the vehicle did is the backbone. What the driver entered is evidence reconciled against it. |
| PR-5 | **Everything is accounted for.** Counts are conserved from layer to layer, and a build that cannot prove it fails. |
| PR-6 | **Deterministic and idempotent.** The same inputs produce the same outputs, with the same identifiers, every time. Re-running a step is always safe. |
| PR-7 | **The lake is the record; the database serves.** Data lives as Parquet files. PostgreSQL is rebuilt from them at any time. |
| PR-8 | **Evidence is explicit.** Every inferred fact carries a method, a confidence, and the evidence behind it. "Unknown" is a valid answer. |
| PR-9 | **Measured, not assumed.** Thresholds come from the data and are recorded with their justification. Quality gates are numbers, checked automatically. |
| PR-10 | **Right-sized tooling.** One host, standard open-source tools, no platform the team cannot operate alone. |

## 6. Key decisions

Each decision is elaborated in the document named in the last column.

| ID | Decision | Why | Where |
| --- | --- | --- | --- |
| D-01 | Five layers: raw, bronze, silver, inference, gold | One job per layer; conventional medallion layout with inference as a named part of the silver tier | 02 |
| D-02 | Data is stored as Parquet in a file lake; PostgreSQL with PostGIS is the serving database | Columnar files are an order of magnitude smaller and faster for telemetry, and make the database disposable | 02, 10 |
| D-03 | Heavy computation runs outside the database (Polars, DuckDB, Python) | Per-vehicle sequential algorithms and large scans do not belong in a row store | 02, 10 |
| D-04 | Raw files are fetched from the remote raw store, read-only, verified by checksum | The pipeline never assumes files are already on the machine | 04 |
| D-05 | Bronze is keyed by source file and keeps every row and field as text | Removes the whole class of overwrite and silent-drop defects | 04 |
| D-06 | The observed vehicle timeline is the backbone; operator trip records are reconciled against it | The GPS track is what happened; the trip record is what someone typed | 07 |
| D-07 | Bus and device linkage combines several kinds of evidence; no single source is required | Each evidence source is missing for part of the fleet | 07 |
| D-08 | Route patterns are first-class and can be learned from observation | Schedules are incomplete and sometimes wrong | 07 |
| D-09 | Accounting invariants fail the build | "No data lost" must be proven on every run | 09 |
| D-10 | Identifiers are deterministic functions of natural keys | Identifiers survive rebuilds, so saved analyses keep working | 02 |
| D-11 | Published data changes only by promoting a versioned release | Research results stay reproducible while models improve | 09 |
| D-12 | Models are released through gates measured on a frozen, human-labeled test set | Self-improvement without drifting into confident error | 07, 16 |
| D-13 | Fare card identifiers are pseudonymized in gold; raw identifiers stay in restricted silver | Travel histories are personal data | 11 |
| D-14 | One repository, several packages, managed only with uv | Clear boundaries without cross-repository version coupling | 13 |
| D-15 | Plain SQL and Python; no transformation framework; no orchestrator service at first | Fewer moving parts; revisit by decision record | 13, 15 |
| D-16 | `develop` is the protected default branch, merged into by squash; `main` is the production branch, where every commit is a release; an automated release pull request from `develop` into `main`, merged with a merge commit; `main` is merged back into `develop` after every release | One commit per change on `develop`, a visible and deliberate act of releasing, a changelog in the repository, and two branches that stay level | 14 |
| D-17 | One host with three storage tiers; local backups, with an off-machine copy required before the system is declared production-ready | Matches the hardware that exists while naming the gap honestly | 03, 12 |
| D-18 | Build order: one reference month first (2023-11), then all of 2023, then every available year | A complete vertical slice proves the design before it is scaled | 17 |

## 7. What "done" means

The system is complete in three milestones, detailed in the
[roadmap](17-roadmap.md):

- **M1, reference month.** November 2023 is built end to end from raw
  files, passes every accounting invariant, meets the inference quality
  gates on the frozen test set, and is published as the first release.
- **M2, reference year.** All of 2023 is built and released with the same
  guarantees, within the performance budgets.
- **M3, full history.** Every year present in the raw store is built and
  released, and the improvement loop (flag, review, retrain, gated release)
  has completed at least one full cycle.

The system is **production-ready** when M1 is met, backups are restorable
from a physical disk other than the one holding the data, and an
off-machine copy of the irreplaceable data exists (see `BKP-12`).
