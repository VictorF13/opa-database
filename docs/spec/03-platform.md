# 03. Platform

This document specifies the host, storage, containers, and database
configuration. Everything here is expressed as files in the repository
under `deploy/`, so the platform is reproducible and reviewable.

## 1. Reference host

The system runs on one Linux server. Performance budgets in
[10-performance.md](10-performance.md) are stated for this reference host:

| Resource | Reference value |
| --- | --- |
| CPU | 24 cores |
| Memory | 60 GB |
| Storage | NVMe solid-state for the fast tier; additional disks for bulk and backup |
| Network | Private overlay network (Tailscale); no public exposure |

- **PLT-1 (MUST)** The host's clock is synchronized (NTP) and containers
  run in UTC.
- **PLT-2 (MUST)** All paths, ports, and addresses the system uses are
  configuration (section 2), never literals in code.

## 2. Storage tiers

Storage is described as three tiers. Each tier is a directory configured
by an environment variable. Which physical disk backs each tier is a
deployment decision, recorded in the runbook.

| Tier | Variable | Holds | Wants |
| --- | --- | --- | --- |
| FAST | `OPA_FAST_ROOT` | PostgreSQL data directory; DuckDB temporary files | Solid-state, low latency |
| BULK | `OPA_BULK_ROOT` | Raw mirror, the lake, model artifacts | Capacity; sequential throughput |
| BACKUP | `OPA_BACKUP_ROOT` | Backup repository | A different physical device from the data it protects |

- **PLT-3 (MUST)** FAST is on solid-state storage.
- **PLT-4 (MUST)** A preflight command (`opa ops preflight`) reports, for
  each tier, the backing block device, filesystem, free space, and whether
  BACKUP shares a physical device with FAST or BULK.
- **PLT-5 (MUST)** When BACKUP shares a physical device with the data it
  protects, backups still run, every run is recorded with the status
  `same-device`, and the system reports itself as not production-ready
  (`BKP-12`).
- **PLT-6 (MUST)** No step writes outside the three tier roots and the
  repository checkout.
- **PLT-7 (SHOULD)** When more than one disk is available for BULK or
  BACKUP, they are mirrored (ZFS mirror or RAID 1). Capacity is added by
  growing the tier, not by scattering paths.
- **PLT-8 (MUST)** Free space on every tier is checked before each
  pipeline run and each backup. A run that would exceed
  `ops.min_free_fraction` (default 10% free) refuses to start.
- **PLT-9 (SHOULD)** Disk health is monitored (SMART) and reported by the
  health check (`OPS-31`).

## 3. Containers

Services run under Docker Compose from `deploy/compose.yaml`.

| Service | Purpose |
| --- | --- |
| `postgres` | PostgreSQL with PostGIS |
| `sqlweb` | A web SQL client for people without a local client |
| `app-labeling` | The labeling application |
| `app-explorer` | The exploration and feedback application |

- **PLT-10 (MUST)** The compose file declares an explicit project name
  (`name: opa`). The project name never depends on the directory.
- **PLT-11 (MUST)** Every service sets `restart: unless-stopped`, and the
  Docker daemon is enabled at boot, so services return after a reboot
  without human action.
- **PLT-12 (MUST)** Persistent data uses bind mounts to explicit paths
  under the tier roots. No named or anonymous volumes hold data, so no
  compose command can delete it.
- **PLT-13 (MUST)** The `postgres` service sets `shm_size` to at least
  8 GB. Parallel queries exchange data through shared memory, and the
  container default (64 MB) makes large parallel joins fail.
- **PLT-14 (MUST)** The `postgres` service has a health check
  (`pg_isready`) and a `stop_grace_period` of at least 2 minutes.
- **PLT-15 (MUST)** Images are pinned by digest. Updating an image is a
  reviewed change.
- **PLT-16 (MUST)** Published ports bind to the address in `OPA_BIND_HOST`
  (the host's private overlay address in production, `127.0.0.1`
  otherwise). Ports are configuration (`OPA_PG_PORT`, `OPA_SQLWEB_PORT`,
  and one per application). Nothing binds to all interfaces.
- **PLT-17 (MUST)** Container logs are size-limited and rotated.
- **PLT-18 (MUST)** No credential appears in the compose file or in an
  image. Credentials come from files outside the repository (`SEC-30`).

## 4. PostgreSQL

### 4.1 Version

- **PLT-20 (MUST)** PostgreSQL 18 with PostGIS 3.6, from the official
  PostGIS image. The policy for later upgrades is: the newest major
  version that has been generally available for at least six months and
  has an official PostGIS image.
- **PLT-21 (MUST)** Minor (patch) releases are applied within 30 days of
  publication by updating the pinned digest.
- **PLT-22 (MUST)** The data directory is mounted where the image
  documents it for the major version in use, on the FAST tier, under a
  path that includes the major version (for example
  `${OPA_FAST_ROOT}/postgres/18`).

### 4.2 Extensions

| Extension | Use | Status |
| --- | --- | --- |
| `postgis` | Geometry types, spatial indexes and functions | Required |
| `btree_gist` | Combined equality and range indexes; exclusion constraints | Required |
| `pg_stat_statements` | Query statistics | Required |
| `pgcrypto` | Keyed hashing inside the database when needed | Required |
| `pg_duckdb` | Reading the lake from inside PostgreSQL | Optional (`PLT-24`) |

- **PLT-23 (MUST)** Only the extensions in this table are installed. In
  particular, no geocoder or topology extension is installed.
- **PLT-24 (MAY)** `pg_duckdb` is adopted if a time-boxed evaluation in
  phase P1 shows that, on the chosen image, views over lake Parquet files
  can be created, granted to read-only roles, and queried with partition
  pruning. If adopted, the lake is mounted read-only into the container
  and exposed as views in a schema named `lake`. If not adopted, the lake
  is queried with DuckDB directly (`OPS-14`), and nothing else in this
  specification changes.

### 4.3 Configuration

Configuration lives in `deploy/postgres/postgresql.conf`, mounted
read-only. The values below are for the reference host, where PostgreSQL
shares memory and cores with the pipeline (section 5).

| Setting | Value | Reason |
| --- | --- | --- |
| `shared_buffers` | `12GB` | About 20% of memory; the rest is left to the page cache and to pipeline jobs |
| `effective_cache_size` | `36GB` | What the planner may assume is cached |
| `work_mem` | `64MB` | Per sort or hash node; raised per role for analysts (`SEC-9`) |
| `hash_mem_multiplier` | `2.0` | Hash joins may use twice `work_mem` |
| `maintenance_work_mem` | `2GB` | Index builds on partitions with over 100 million rows |
| `max_worker_processes` | `24` | One per core |
| `max_parallel_workers` | `16` | Leaves cores for other sessions |
| `max_parallel_workers_per_gather` | `6` | Parallel scans and joins |
| `max_parallel_maintenance_workers` | `6` | Parallel index builds |
| `enable_partitionwise_join` | `on` | Facts share partition bounds (`PERF-20`) |
| `enable_partitionwise_aggregate` | `on` | Same |
| `random_page_cost` | `1.1` | Solid-state storage |
| `effective_io_concurrency` | `200` | Solid-state storage |
| `default_statistics_target` | `200` | Better estimates on skewed keys |
| `max_wal_size` | `32GB` | Bulk loads must not force a checkpoint every gigabyte |
| `min_wal_size` | `4GB` | Avoid recycling churn |
| `checkpoint_timeout` | `15min` | Fewer, smoother checkpoints |
| `checkpoint_completion_target` | `0.9` | Spread checkpoint writes |
| `wal_compression` | `zstd` | Smaller write-ahead log during loads |
| `max_connections` | `60` | A small, known set of clients |
| `shared_preload_libraries` | `pg_stat_statements` | Plus `pg_duckdb` if adopted |
| `password_encryption` | `scram-sha-256` | |
| `timezone`, `log_timezone` | `UTC` | |
| `log_min_duration_statement` | `5s` | Record slow queries |
| `log_connections`, `log_disconnections` | `on` | Record who connects |
| `log_checkpoints`, `log_lock_waits` | `on` | Diagnose load stalls and blocking |
| `log_statement` | `ddl` | Record every schema change |
| `log_temp_files` | `1GB` | Find queries that spill to disk |
| `log_line_prefix` | `%m [%p] %q%u@%d/%a` | Time, process, user, database, application |
| `idle_in_transaction_session_timeout` | `30min` | Abandoned transactions do not hold locks forever |
| `autovacuum_max_workers` | `4` | |

- **PLT-25 (MUST)** Every setting that differs from the PostgreSQL default
  appears in the configuration file with a one-line reason. Nothing is
  changed with `ALTER SYSTEM`.
- **PLT-26 (MUST)** `statement_timeout` is not set globally. It is set per
  role (`SEC-9`), so the pipeline is never cut off and analysts cannot run
  unbounded queries.
- **PLT-27 (MUST)** Client authentication requires SCRAM for every network
  connection. Passwordless (`trust`) access is limited to the local socket
  inside the container.
- **PLT-28 (SHOULD)** After the first month is published, settings are
  re-derived from measurements (`pg_stat_statements`, checkpoint and
  temporary-file logs) and this table is updated.

## 5. Sharing the host between the database and the pipeline

| Consumer | Memory budget |
| --- | --- |
| PostgreSQL shared buffers | 12 GB |
| PostgreSQL query memory | Up to about 8 GB |
| Pipeline compute (DuckDB memory limit plus worker processes) | 28 GB (`ops.compute_memory_gb`) |
| Operating system cache and headroom | The remainder |

- **PLT-30 (MUST)** Pipeline compute respects `ops.compute_memory_gb` and
  `ops.compute_workers` (default 20). DuckDB sessions set an explicit
  memory limit, thread count, and a temporary directory on FAST.
- **PLT-31 (MUST)** Heavy tasks (a month of inference, a publish, a backup
  of the lake) take a host-wide exclusive lock so that at most one runs at
  a time.

## 6. Observability

- **PLT-40 (MUST)** PostgreSQL logs are kept for at least 30 days.
- **PLT-41 (MUST)** `pg_stat_statements` is enabled, and a weekly report
  of the slowest and most frequent queries is written to `meta`
  (`OPS-33`).
- **PLT-42 (MUST)** The health check (`OPS-31`) covers: services up,
  database accepting connections, free space per tier, last backup age and
  status, last pipeline run status, disk health.

## 7. Capacity plan

Planning estimates, to be replaced by measurements after milestone M1.

| Item | Estimate per month of data |
| --- | --- |
| Lake: bronze AVL | 2 GB |
| Lake: silver AVL | 2 GB |
| Lake: bronze and silver AFC | 0.5 GB |
| Lake: inference, per retained build | 1.5 GB |
| Lake: gold, per retained build | 1.5 GB |
| Database: gold and inference | 8 GB |
| Database: silver AFC | 5 GB |
| Database: silver AVL (optional publication) | 20 GB |

The raw mirror's size is unknown until the inventory (`RAW-3`).

- **PLT-50 (MUST)** The set of months published to the serving database is
  configurable per table group (`publish.window.*`). Defaults: gold and
  inference for every released month; silver AFC for every released month;
  silver AVL for the reference year only.
- **PLT-51 (MUST)** `opa ops capacity` projects tier usage for a requested
  window from measured sizes and refuses a publish that would not fit.

## 8. Acceptance

The platform is accepted when:

1. `opa ops preflight` passes and reports the device behind each tier.
2. A reboot of the host brings every service back without intervention.
3. A parallel hash join over a table of at least 100 million rows
   completes with parallel workers enabled.
4. A deliberate `docker compose down` followed by `up` loses no data.
5. Every non-default setting is in the repository with its reason.
