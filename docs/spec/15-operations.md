# 15. Operations

This document specifies how the system is run day to day: the command
line interface, how work is sequenced, what runs on a schedule, how
health is observed, and which procedures are written down.

## 1. Command line interface

- **OPS-1 (MUST)** One command, `opa`, is the only way the pipeline is
  operated. There are no steps that exist only as notebooks, ad hoc
  scripts, or manual SQL.

```text
opa raw       inventory | fetch | verify
opa bronze    ingest | verify
opa silver    build
opa ref       validate | load
opa infer     run [component] | eval | train
opa gold      build
opa check     run
opa build     month <YYYY-MM>
opa run       month <YYYY-MM> | range <from> <to>
opa release   create | gate | publish | pin | unpin | list | notes
opa db        migrate | roles apply | access verify | rebuild
opa lake      shell | catalog
opa labels    snapshot | import
opa lineage   <table> <key>
opa backup    run | verify | restore-test | status
opa bench
opa meta      rebuild
opa ops       preflight | status | health | capacity | deploy
```

- **OPS-2 (MUST)** Every command is idempotent, accepts `--dry-run` to
  show what it would do, logs in structured form, records a `task_run`
  (`DQ-1`), and exits non-zero on failure.
- **OPS-3 (MUST)** Scope is selected the same way everywhere: `--month`,
  `--from` and `--to`, `--source`, `--table`.
- **OPS-4 (MUST)** `opa run month` executes the whole chain for a month,
  from fetching raw files to a finished build, doing only what is stale
  (`ARC-52`). Running it twice in a row does nothing the second time.
- **OPS-5 (MUST)** `opa build month` creates a new build for a month
  (inference, gold, checks, evaluation). `opa release create` assembles
  a release from finished builds; `opa release gate` evaluates it;
  `opa release publish` loads it into the serving database.
- **OPS-6 (MUST)** `opa release publish <release>` can publish any
  retained release, which is also how a release is rolled back.
  `opa db rebuild` reloads every served table of the current release
  from the lake.
- **OPS-7 (MUST)** A command that removes or replaces data states what it
  will affect and requires an explicit confirmation flag. Commands
  refuse to act on protected resources (`COX-3`).
- **OPS-8 (MUST)** Every command and option has help text. The command
  reference in the documentation is generated from it (`ENG-58`).

## 2. Sequencing work

- **OPS-10 (MUST)** Tasks declare their inputs and outputs. The order of
  execution is derived from those declarations, not written by hand.
- **OPS-11 (MUST)** A failed task fails its run, leaves earlier outputs
  intact, leaves no partial output of its own (`ARC-12`), and can be
  resumed by running the same command again.
- **OPS-12 (MUST)** Heavy tasks take the host-wide lock (`PLT-31`). A
  second heavy task waits or exits with a clear message, as requested.
- **OPS-13 (MUST)** Every run prints progress as it goes: which task,
  which partition, how many done and remaining, elapsed and estimated
  time. Nothing runs silently for more than a minute.
- **OPS-14 (MUST)** Analysts can query the lake directly. `opa lake
  catalog` maintains a DuckDB catalog file with a read-only view for
  every lake table of the published release, including bronze and the
  ping-level tables that are not in the serving database. `opa lake
  shell` opens it. The same catalog works from Python and from SQL
  clients that support DuckDB.

An orchestrator service is deliberately not used (`D-15`): the command
line interface with computed staleness and systemd timers covers a single
host. The decision is revisited if runs need a web console, distributed
workers, or complex retry policies.

## 3. Scheduled work

Timers are systemd units in `deploy/systemd/`.

| Timer | Frequency | Action |
| --- | --- | --- |
| `opa-raw-inventory` | Daily | List the remote raw store; report new, changed, and missing files |
| `opa-raw-verify` | Monthly | Re-hash the raw mirror against the manifest |
| `opa-backup-hourly` | Hourly | Class A database dump |
| `opa-backup-daily` | Daily | Class A export, class C snapshot, roles dump, retention |
| `opa-backup-offsite` | Daily | Copy to the off-machine repository, when configured |
| `opa-restore-test` | Weekly | Automated restore test |
| `opa-health` | Every 15 minutes | Health check |
| `opa-query-report` | Weekly | Slowest and most frequent queries; unused indexes |
| `opa-release-check` | Daily | Report a new code release available for deployment |

- **OPS-20 (MUST)** Every scheduled action is one `opa` command, so it
  can be run by hand identically.
- **OPS-21 (MUST)** `opa raw verify` runs at least monthly and after any
  storage incident.
- **OPS-22 (MAY)** Ingest through silver may be scheduled to follow the
  daily inventory when new files appear (`ops.auto_ingest`). Builds and
  releases are always started by a person.
- **OPS-23 (MUST)** A scheduled action that fails is retried at its next
  slot and raises an alert. Timers that were missed while the host was
  off run at boot.

## 4. Health and alerting

- **OPS-30 (MUST)** `opa ops status` prints, on one screen: services,
  published release, last run and its result, backup age and status per
  class, free space per tier, open alerts, and each
  production-readiness criterion (`BKP-12`).
- **OPS-31 (MUST)** `opa ops health` checks the items of `PLT-42` and
  `BKP-15`, writes the result to `meta.ops_event`, and exits non-zero
  when something is wrong.
- **OPS-32 (MUST)** Alerts have two levels. A critical alert (failed
  backup, integrity mismatch, service down, tier nearly full) is pushed
  to the operator through the configured notification channel. A warning
  appears in `opa ops status` and in the weekly summary. With no channel
  configured, alerts are still recorded and shown.
- **OPS-33 (MUST)** The weekly query report lists, from
  `pg_stat_statements`, the queries with the highest total time and the
  highest mean time, and indexes with no scans. It is the input to
  `PERF-27` and `PLT-28`.
- **OPS-34 (MUST)** Tier usage is recorded daily and projected forward.
  A tier predicted to fill within 60 days raises a warning.
- **OPS-35 (MUST)** `meta.ops_event` is the operations log: deployments,
  role changes, uses of the administrator account, restores, manual
  interventions, each with who, when, and why.

## 5. Runbooks

- **OPS-40 (MUST)** Each procedure below is a file in `docs/runbooks/`
  with prerequisites, exact commands, how to verify success, and how to
  undo. A runbook is tested by following it on a scratch environment
  before it is relied on.

| Runbook | When |
| --- | --- |
| Bring up the platform | New host or after hardware change |
| Process a new month | New raw data has arrived |
| Create and publish a release | A build is ready |
| Roll back a release | A published release is wrong |
| Recover from each scenario of `12-backup-and-recovery.md` | Failure |
| Add or remove a person | Team change |
| Rotate a secret | On schedule or after exposure |
| Upgrade PostgreSQL (minor, major) | New version |
| Deploy a code release | A release is available |
| Move a tier to a new disk | New hardware |
| Handle a failed check or gate | A build or release is blocked |
| Review proposals, flags, and code meanings | Weekly |
| Quarterly access review and recovery drill | Quarterly |

## 6. Acceptance

1. A person who has never operated the system processes a new month and
   publishes a release using only the runbooks.
2. Killing a run halfway and running the same command again completes
   the work with identical results.
3. `opa ops status` shows a deliberately broken backup and a nearly full
   tier as alerts.
4. Every timer's action succeeds when run by hand.
