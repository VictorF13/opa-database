# 12. Backup and recovery

The architecture makes most data rebuildable. Backup therefore
concentrates on what cannot be rebuilt, and recovery is defined as a set
of rehearsed procedures, not as a hope.

## 1. What needs protecting

| Class | Data | Can it be rebuilt? |
| --- | --- | --- |
| A | Human judgments (`labels`, `feedback`), the metadata history (`meta`), secrets | No |
| B | Raw files | No, but they are immutable and the remote raw store is a second copy |
| C | Released builds (inference and gold of retained releases), model artifacts | Yes, at a cost of hours to days |
| D | Bronze, silver, unreleased builds, every served table | Yes, from B plus the repository |

Reference data, role definitions, configuration, and the platform
definition are in the repository and are protected by it.

## 2. Objectives

| Class | Maximum data loss | Maximum time to recover |
| --- | --- | --- |
| A | 1 hour | 1 hour |
| B | None (immutable, integrity verified monthly) | Time to fetch again from the remote store |
| C | 1 day | 1 day |
| D | Not applicable | Rebuild time: at most 2 days per year of data (`PERF-40`) |

## 3. Method

- **BKP-1 (MUST)** Backups are written to a restic repository on the
  BACKUP tier. The repository is encrypted, so a disk that leaves the
  building discloses nothing.
- **BKP-2 (MUST)** Backups run from systemd timers defined in
  `deploy/systemd/`. Each run writes a row to `meta.backup_run` with its
  scope, size, duration, status, and whether BACKUP shared a physical
  device with the data (`PLT-5`).
- **BKP-3 (MUST)** Class A, database state: a logical dump of `labels`,
  `feedback`, and `meta` is taken hourly, and a dump of roles and
  memberships daily. Dumps are consistent snapshots and are added to the
  repository.
- **BKP-4 (MUST)** Class A is also exported as Parquet to
  `lake/exports/` daily, in the same form used for label snapshots
  (`APP-25`), so the lake alone can restore it (`ARC-5`).
- **BKP-5 (MUST)** Class C: the build directories of every retained
  release and the model artifact store are snapshotted daily.
- **BKP-6 (MUST)** Secrets (the pseudonymization key, credentials) are
  backed up to a separate repository with a separate password, never
  alongside the data they protect. The owner also keeps one offline copy
  of the pseudonymization key and of the backup passwords.
- **BKP-7 (SHOULD)** Class B: the raw mirror is snapshotted when BACKUP
  has capacity for it. Until then its protection is the remote store
  plus monthly integrity verification (`RAW-11`).
- **BKP-8 (MUST)** Retention: hourly snapshots for 48 hours, daily for
  30 days, weekly for 12 weeks, monthly for 12 months. Snapshots of
  pinned releases (`DQ-28`) are kept while the release is pinned.
- **BKP-9 (MAY)** Continuous archiving of the database's write-ahead log
  (point-in-time recovery) may be added if a loss window shorter than
  one hour is ever required. It is not needed while the only
  non-rebuildable database state is the small class A set.
- **BKP-10 (MUST)** A restore never overwrites live data. Data is
  restored to a new location, verified, and then swapped in.

## 4. Proof

- **BKP-11 (MUST)** Every backup run ends with a verification of the
  repository's structure and of a sample of its data.
- **BKP-12 (MUST)** The system is **production-ready** only when all of
  the following hold. `opa ops status` reports the state of each.
  1. BACKUP is on a physical device that holds neither FAST nor BULK.
  2. A copy of class A exists off the machine and has been restored
     successfully at least once.
  3. The weekly restore test (`BKP-13`) has passed four times in a row.
  4. Every recovery procedure in section 5 has been rehearsed once.
- **BKP-13 (MUST)** A weekly automated test restores the latest class A
  dump into a scratch database and compares row counts and content
  digests with the live tables. The result is recorded and a failure is
  a critical alert.
- **BKP-14 (MUST)** A quarterly drill rebuilds the serving database from
  the lake on a scratch instance, restores class A into it, and verifies
  access (`SEC-11`). The time taken is recorded as the measured recovery
  time.
- **BKP-15 (MUST)** The health check (`OPS-31`) reports the age and
  status of the last backup of each class. A class A backup older than
  3 hours, or any failed backup, raises an alert.

## 5. Recovery procedures

Each scenario has a runbook in `docs/runbooks/` with exact commands.

| Scenario | Procedure in brief |
| --- | --- |
| A label or flag was deleted or corrupted | Restore class A to a scratch database; copy the affected rows back as new rows |
| A release is wrong | Publish the previous retained release (`OPS-6`) |
| The serving database is lost (FAST fails) | Start a new instance from the platform definition; apply migrations and roles; restore class A; publish the current release from the lake; verify access |
| The lake is lost (BULK fails) | Restore retained releases and models from backup; publish; fetch raw again; rebuild bronze and silver in the background |
| The backup disk is lost | Replace it; run a full backup; the off-machine copy covers the gap |
| The host is lost | Provision a host from the platform definition; restore class A and C from the off-machine copy; fetch raw; rebuild |
| The pseudonymization key is lost | Restore it from the secrets backup or the offline copy; if truly lost, follow `SEC-22` |

## 6. Off-machine copy

- **BKP-20 (MUST)** A second repository, on another machine or on a
  removable disk kept elsewhere, receives class A daily and class C
  weekly. It is not the remote raw store: nothing is ever written there
  (`RAW-2`).
- **BKP-21 (MUST)** Until the off-machine copy exists, the gap is listed
  as an open item with an owner, and the system reports itself as not
  production-ready.

## 7. Before the backup disks exist

The design does not wait for hardware. Backups are implemented, scheduled,
and tested from phase P1 with whatever BACKUP is configured to be:

- If BACKUP is a separate physical device from the start, criterion 1 of
  `BKP-12` is met immediately.
- If it is not, backups still protect against the most likely accidents
  (a wrong command, a bad build, a corrupted table) and every run is
  marked `same-device`. Moving BACKUP to its own device later is a
  configuration change followed by a full backup.

## 8. Acceptance

1. A class A row deleted on purpose is recovered within the hour, with
   at most one hour of other work lost.
2. The serving database is destroyed on a scratch host and recovered by
   the runbook alone.
3. `opa ops status` reports each production-readiness criterion
   truthfully.
