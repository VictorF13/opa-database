# OPA Database: system specification

| Field | Value |
| --- | --- |
| Status | Draft 1, for review |
| Date | 2026-10-04 |
| Owner | Victor Abreu |
| Language | English (application user interfaces are in Brazilian Portuguese) |

This directory specifies the OPA Database system: a research database that
turns the raw operational data of Fortaleza's bus system into a complete,
trustworthy, fast, and reproducible source of truth.

The specification stands on its own. Every decision in it is derived from
the source data and from the goals in [00-overview.md](00-overview.md). It
is not a migration plan and it does not describe, depend on, or map to any
earlier implementation. The single place where pre-existing deployments are
mentioned is [18-coexistence.md](18-coexistence.md), which only states how
the system is kept isolated from whatever already runs on the same host.

## Documents

Read in order the first time. Each document can also be read alone.

| # | Document | What it defines |
| --- | --- | --- |
| 00 | [Overview](00-overview.md) | Purpose, goals, non-goals, principles, key decisions |
| 01 | [Source data](01-source-data.md) | The raw sources, their formats, and what is known about them |
| 02 | [Architecture](02-architecture.md) | Layers, storage, compute, identifiers, time, builds and releases |
| 03 | [Platform](03-platform.md) | Host, storage tiers, containers, PostgreSQL configuration |
| 04 | [Raw and bronze](04-raw-and-bronze.md) | Fetching raw files, the manifest, lossless bronze |
| 05 | [Silver](05-silver.md) | Typed, cleaned, conformed tables per source |
| 06 | [Reference data](06-reference-data.md) | Curated lists: companies, zones, overrides, code dictionaries, parameters |
| 07 | [Inference](07-inference.md) | Vehicle timelines, bus and device linkage, patterns, reconciliation, stop events |
| 08 | [Gold](08-gold.md) | The analysis-ready data model |
| 09 | [Quality and lineage](09-quality-and-lineage.md) | Checks, accounting, metadata, builds, releases |
| 10 | [Performance](10-performance.md) | Physical design and performance budgets |
| 11 | [Access and security](11-access-and-security.md) | Roles, privileges, privacy, secrets, network |
| 12 | [Backup and recovery](12-backup-and-recovery.md) | What is protected, how, and how recovery is proven |
| 13 | [Engineering](13-engineering.md) | Repository layout, tooling, code, tests, documentation standards |
| 14 | [Delivery](14-delivery.md) | Branching, commits, pull requests, issues, CI, releases, deployment |
| 15 | [Operations](15-operations.md) | Command line interface, scheduling, monitoring, runbooks |
| 16 | [Applications and feedback](16-apps-and-feedback.md) | Labeling and exploration apps, labels, the improvement loop |
| 17 | [Roadmap](17-roadmap.md) | Build phases with acceptance criteria |
| 18 | [Coexistence](18-coexistence.md) | Isolation from pre-existing deployments on the host |
| 19 | [Open items and risks](19-open-items-and-risks.md) | Decisions still open, each with a default, and the risk register |
| | [Glossary](glossary.md) | Terms used throughout |

## Conventions

### Normative language

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are used as
defined in RFC 2119 and RFC 8174. A statement without one of these words is
explanation, not a requirement.

### Requirement identifiers

Every requirement has an identifier of the form `AREA-n`, for example
`BRZ-4`. Identifiers are stable: they are never renumbered or reused. A
requirement that no longer applies is marked "withdrawn" and kept in place.
Code, tests, issues, and pull requests refer to requirements by identifier.

| Prefix | Area | Document |
| --- | --- | --- |
| `ARC` | Architecture | 02 |
| `PLT` | Platform | 03 |
| `RAW` | Raw store | 04 |
| `BRZ` | Bronze | 04 |
| `SLV` | Silver | 05 |
| `REF` | Reference data | 06 |
| `INF` | Inference | 07 |
| `GLD` | Gold | 08 |
| `DQ` | Quality and lineage | 09 |
| `PERF` | Performance | 10 |
| `SEC` | Access and security | 11 |
| `BKP` | Backup and recovery | 12 |
| `ENG` | Engineering | 13 |
| `DLV` | Delivery | 14 |
| `OPS` | Operations | 15 |
| `APP` | Applications and feedback | 16 |
| `COX` | Coexistence | 18 |

### Parameters

Every tunable value (a threshold, a tolerance, a window) is a named
parameter, written like `track.max_gap_s`. A parameter has one default, one
owner document, and a recorded justification. Parameters live in versioned
configuration (see `REF-20`), never as literals scattered through code.

### Profile facts

Statements marked **Profile** are measurements of the source data taken on
a sample (November 2023 unless stated otherwise). They explain why the
design looks the way it does. They are not guarantees: phase P0 of the
[roadmap](17-roadmap.md) re-measures every one of them on the full raw
store, and a profile fact that turns out to be wrong is corrected here
before the design that relies on it is built.

## Changing this specification

- The specification is changed by pull request, like code.
- A change of design direction is recorded as a decision record in
  `docs/adr/` (see `ENG-52`) and then reflected here.
- When code and specification disagree, that is a defect in one of them.
  It is resolved in the same pull request that discovers it.
- Open questions are tracked in
  [19-open-items-and-risks.md](19-open-items-and-risks.md). Each has a
  default so that work is never blocked on an unanswered question.
