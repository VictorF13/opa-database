# 19. Open items and risks

## 1. Open items

An open item is a question whose answer is not yet known. Each has a
default, so that work proceeds, and a phase by which it must be settled.
Settling an item means updating the specification and, where it is a
design choice, writing a decision record.

| ID | Question | Default until settled | Settle by |
| --- | --- | --- | --- |
| O-1 | What exactly is in the remote raw store: years per source, sizes, formats? | Discover with the inventory (`RAW-3`) | P2 |
| O-2 | Which physical disks back the FAST, BULK, and BACKUP tiers? | FAST on a dedicated solid-state disk; BACKUP reported as `same-device` until it has its own disk (`PLT-5`) | P1 |
| O-3 | Where does the off-machine backup copy live? | Nowhere yet; the system reports itself as not production-ready (`BKP-12`). Candidates: a second machine, a removable disk kept elsewhere. Not the remote raw store. | M1 |
| O-4 | What do the fare codes mean (passenger type, integration, and the rest)? | `unknown`; meanings inferred from the profile (`INF-80`) and confirmed with the agency when possible | P7 |
| O-5 | Do the supervisor and the agency agree with the privacy design (`SEC-20` to `SEC-24`)? | The design as specified | P4 |
| O-6 | What are the legal basis, the data sharing terms, and the retention period? | Treat all tap-level data as personal; record what is known in the governance document (`SEC-27`) | P4 |
| O-7 | Who owns the remote raw store account in the long term? | Mirror everything locally as early as possible, so that losing the account is not fatal | P2 |
| O-8 | Is `pg_duckdb` adopted (`PLT-24`)? | No; the lake is queried through the DuckDB catalog (`OPS-14`) | P1 |
| O-9 | What are the units of AVL speed and odometer? | Speed in km/h and odometer in meters, to be verified | P4 |
| O-10 | Is 03:00 local the right operational-day cutoff? | 03:00 | P4 |
| O-11 | Which Python minor version? | The newest supported by every runtime dependency (`ENG-16`) | P0 |
| O-12 | Which channel carries critical alerts? | None; alerts are recorded and shown in `opa ops status` | P1 |
| O-13 | Does the fare source contain trip records with no taps? | Handled if present (`BRZ-21`, `SLV-30`) | P3 |
| O-14 | What do earlier raw formats look like, and how do they map to silver? | Bronze datasets are defined when found (`BRZ-24`); silver mapping is done in P10 | P10 |
| O-15 | Where are label snapshots kept besides the host? | In the backup repository and its off-machine copy; never in the code repository | P1 |
| O-16 | How were imported judgments made (random sample or not, predictions shown or not)? | Imported labels are used for training and comparison only, unless documented as a random sample (`APP-26`) | P5 |
| O-17 | Which source provides the holiday calendar? | A list of national, state, and municipal public holidays maintained in `ref` | P4 |
| O-18 | Can the overlay network's proxy provide identity headers to the applications? | Applications refuse to save without an identified person; fallback is a per-person application login | P5 |
| O-19 | When is earlier code removed from the working tree? | At P0, after tagging and preserving it on a maintenance branch (`COX-13`) | P0 |
| O-20 | How many months of `silver.avl_pings` are published in the serving database? | The reference year (`PLT-50`) | P10 |
| O-21 | What is the exact service-area box? | The initial box of `geo.area_bbox`, widened to the full metropolitan service area after profiling | P4 |
| O-22 | What may leave the project, and in what form? | Aggregates with at least 10 cards per cell, or an explicit recorded review (`SEC-24`) | P8 |

## 2. Risks

| ID | Risk | Impact | Mitigation |
| --- | --- | --- | --- |
| R-1 | The remote raw store is the only complete copy of the raw files until they are mirrored | Permanent loss of source data | Mirror and verify in P2; monthly integrity checks; raw included in backups when capacity allows |
| R-2 | Backups share a physical disk with the data at the start | A disk failure loses class A data | Every run marked `same-device`; production-readiness gate; move BACKUP to its own disk as soon as it exists |
| R-3 | One host | A hardware failure stops everything | Everything but class A and B is rebuildable; off-machine copy of class A; a runbook for a new host |
| R-4 | One maintainer | Knowledge is lost if they are unavailable | This specification, runbooks, tests, generated references, and guidance for coding agents |
| R-5 | Inference does not reach its gates | M1 is delayed | Transparent baselines first; risks tested in order (`17-roadmap.md`, section 4); "unknown" is allowed; gates are revised only by decision record with evidence |
| R-6 | Labels are biased by what the labeler was shown or by how cases were chosen | Evaluation overstates quality | Named streams, hidden predictions for the random stream, frozen test set from random samples only, adjudication |
| R-7 | A source changes format or content without notice | Silent loss or misreading | Lossless bronze, drift events, quarantine, plausibility checks |
| R-8 | The full history does not fit on the available storage | Scale-out stalls | Capacity projection before every extension; Parquet lake; configurable published window |
| R-9 | Personal data is exposed | Harm to riders; legal exposure | Pseudonymous keys everywhere but restricted silver; encrypted backups; no real data in the repository; quarterly access review |
| R-10 | The pseudonymization key is lost | Card keys cannot be reproduced | Separate secrets backup; offline copy held by the owner |
| R-11 | Building the system disturbs a deployment people depend on | Loss of access or data for current users | The coexistence rules, the protected declaration, and the test that enforces it |
| R-12 | A tool is immature or changes behavior | Rework | Pinned versions; upgrades by reviewed pull request; optional components stay optional |
| R-13 | The specification is larger than the capacity to build it | Nothing finishes | Phases that each deliver something usable; a vertical slice (M1) before scale |
| R-14 | Self-improvement drifts toward confident error | Published data degrades without anyone noticing | Frozen test set, gates, no regression rule, versioned releases, drift monitoring |
| R-15 | Reference data is wrong (a zone, an exception, a code meaning) | Systematic misclassification | Provenance on every row; validation against observation; changes by review |
| R-16 | Operator error | Data loss | Bind mounts instead of volumes; confirmation flags; restore-to-new-location; append-only labels; a disposable serving database |
