# 15. Operations

This document specifies how the system is run day to day: how pipeline
work is launched and sequenced, the command for everything around it,
what runs on a schedule, how health is observed, and which procedures are
written down.

## 1. Two interfaces

| Interface | For |
| --- | --- |
| The orchestrator | Everything that produces data: inventory, fetch, bronze, silver, inference, gold, checks, evaluation, publishing |
| The `opa` command | Everything around the data flow: platform, database structure and roles, releases, labels, backups, deployment |

- **OPS-1 (MUST)** Every step that produces or changes data in the lake
  or in a served table is an asset or job of the orchestrator. There are
  no steps that exist only as notebooks, ad hoc scripts, or manual SQL.
- **OPS-2 (MUST)** Pipeline work is launched from the orchestrator's web
  interface or its own command line. The project adds no second way to
  run it.

```text
opa ops       preflight | status | health | capacity | deploy
opa db        migrate | roles apply | access verify | rebuild
opa release   create | gate | publish | pin | unpin | list | notes
opa labels    snapshot | import
opa lake      shell | maintain
opa lineage   <table> <key>
opa backup    run | verify | restore-test | status
opa bench
```

- **OPS-3 (MUST)** Every `opa` command is idempotent, accepts `--dry-run`
  to show what it would do, logs in structured form, records what it did
  in `ops`, and exits non-zero on failure.
- **OPS-5 (MUST)** `opa release create` records a release for named
  months; `opa release gate` evaluates it; `opa release publish` loads it
  into the serving database by launching the publish job in the
  orchestrator (`DQ-25` to `DQ-29`).
- **OPS-6 (MUST)** `opa release publish <release>` can publish any
  retained release, which is also how a release is rolled back.
  `opa db rebuild` reloads every served table of the published release
  from the lake.
- **OPS-7 (MUST)** A command that removes or replaces data states what it
  will affect and requires an explicit confirmation flag. Commands
  refuse to act on protected resources (`COX-3`).
- **OPS-8 (MUST)** Every command and option has help text. The command
  reference in the documentation is generated from it (`ENG-58`).

## 2. Sequencing work

- **OPS-10 (MUST)** The order of execution is derived from the declared
  dependencies between assets. Nobody writes an order by hand.
- **OPS-4 (MUST)** What runs by itself and what a person starts:

| Work | Started by |
| --- | --- |
| Inventory, fetch, bronze, silver | The orchestrator, automatically, whenever new raw files appear or something upstream changes (`ARC-52`) |
| Inference and gold for a month | A person, for chosen months, or an automation policy the operator enables. Until then the months show as stale |
| A release | A person, always |

  Nothing that runs by itself is visible to readers of the serving
  database (`ARC-40`).
- **OPS-11 (MUST)** A failed run leaves earlier results intact and
  nothing partial of its own (`ARC-12`). It is retried by launching it
  again. Transient failures (the network, the remote store) are retried
  automatically a bounded number of times.
- **OPS-12 (MUST)** Concurrency is governed by the orchestrator
  (`PLT-34`). A second heavy run waits in the queue.
- **OPS-13 (MUST)** Progress is visible while work runs: which partitions
  are done, running, queued, or failed. Long steps log their progress at
  least once a minute.
- **OPS-15 (MUST)** A range of months is processed as one backfill, in
  which each month is a tracked partition that can succeed, fail, and be
  retried on its own.
- **OPS-14 (MUST)** People with access to the host can query the lake
  directly. `opa lake shell` opens a DuckDB session attached read-only to
  the lake at the snapshot of the published release, so it shows the
  same data as the serving database plus the tables that are not
  published (bronze, ping-level inference). Access follows `SEC-45`.

## 3. Scheduled work

Schedules that produce data live in the orchestrator. Protection and
monitoring run from systemd timers in `deploy/systemd/`, so that they
keep working when the orchestrator is down.

| Schedule | Runs in | Frequency | Action |
| --- | --- | --- | --- |
| Raw inventory | Orchestrator | Daily | List the remote raw store; report new, changed, and missing files |
| Raw verification | Orchestrator | Monthly | Re-hash the raw mirror against the manifest |
| Label snapshot | Orchestrator | Daily | Export `labels`, `feedback`, and `ops` to the lake (`BKP-4`) |
| Lake maintenance | Orchestrator | Weekly | Expire snapshots, remove unreferenced files, compact (`DQ-23`) |
| Query report | Orchestrator | Weekly | Slowest and most frequent queries; unused indexes |
| Capacity record | Orchestrator | Daily | Tier usage and projection |
| Retraining check | Orchestrator | Monthly | Start training when enough new labels exist (`APP-41`) |
| `opa-backup-hourly` | systemd | Hourly | Class A database dumps |
| `opa-backup-daily` | systemd | Daily | File snapshots, roles dump, retention |
| `opa-backup-offsite` | systemd | Daily | Copy to the off-machine repository, when configured |
| `opa-restore-test` | systemd | Weekly | Automated restore test |
| `opa-health` | systemd | Every 15 minutes | Health check |
| `opa-release-check` | systemd | Daily | Report a new code release available for deployment |

- **OPS-20 (MUST)** Every scheduled action can be run by hand
  identically: an orchestrator job launched manually, or an `opa`
  command.
- **OPS-21 (MUST)** Raw verification runs at least monthly and after any
  storage incident.
- **OPS-22 (MUST)** Backups, the restore test, and the health check do
  not depend on the orchestrator or on the pipeline image being healthy.
- **OPS-23 (MUST)** A scheduled action that fails is retried at its next
  slot and raises an alert. Timers that were missed while the host was
  off run at boot.

## 4. Health and alerting

- **OPS-30 (MUST)** `opa ops status` prints, on one screen: services,
  published release, failed and stale work, backup age and status per
  class, free space per tier, open alerts, and each
  production-readiness criterion (`BKP-12`).
- **OPS-31 (MUST)** `opa ops health` checks the items of `PLT-42` and
  `BKP-15`, records the result in `ops`, and exits non-zero when
  something is wrong.
- **OPS-32 (MUST)** Alerts have two levels. A critical alert (failed
  backup, integrity mismatch, service down, tier nearly full, failed
  run) is pushed to the operator through the configured notification
  channel, by the orchestrator for runs and by the health check for
  everything else. A warning appears in `opa ops status` and in the
  weekly summary. With no channel configured, alerts are still recorded
  and shown.
- **OPS-33 (MUST)** The weekly query report lists, from
  `pg_stat_statements`, the queries with the highest total time and the
  highest mean time, and indexes with no scans. It is the input to
  `PERF-27` and `PLT-28`.
- **OPS-34 (MUST)** Tier usage is recorded daily and projected forward.
  A tier predicted to fill within 60 days raises a warning.
- **OPS-35 (MUST)** `ops.event` is the operations log: deployments, role
  changes, uses of the administrator account, restores, manual
  interventions, each with who, when, and why.

## 5. Runbooks

- **OPS-40 (MUST)** Each procedure below is a file in `docs/runbooks/`
  with prerequisites, exact steps, how to verify success, and how to
  undo. A runbook is tested by following it on a scratch environment
  before it is relied on.

| Runbook | When |
| --- | --- |
| Bring up the platform | New host or after hardware change |
| Process a new month | New raw data has arrived |
| Run a backfill | Many months need building or rebuilding |
| Create and publish a release | Months are ready |
| Roll back a release | A published release is wrong |
| Recover from each scenario of `12-backup-and-recovery.md` | Failure |
| Add or remove a person | Team change |
| Rotate a secret | On schedule or after exposure |
| Upgrade PostgreSQL (minor, major) | New version |
| Upgrade the lake format, dbt, or the orchestrator | New version |
| Deploy a code release | A release is available |
| Move a tier to a new disk | New hardware |
| Handle a failed check or gate | A month or release is blocked |
| Review proposals, flags, and code meanings | Weekly |
| Quarterly access review and recovery drill | Quarterly |

## 6. Acceptance

1. A person who has never operated the system processes a new month and
   publishes a release using only the runbooks.
2. Killing a run halfway and launching it again completes the work with
   identical results.
3. `opa ops status` shows a deliberately broken backup, a failed run,
   and a nearly full tier as alerts.
4. With the orchestrator stopped, backups and the health check still
   run, and the health check reports the orchestrator as down.
