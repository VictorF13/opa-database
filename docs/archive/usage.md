# Previous pipeline: overview and usage

> **Archived.** This document describes the implementation that predates
> the [specification](../spec/README.md). It is kept for reference only,
> is not a design input for new work, and is out of date in places.
>
> **Do not run `load-silver`, `load-silver-reference`, `build-gold`, or
> `build-diamond` against the database that is in use.** They drop and
> replace data.

## Overview

The pipeline follows a two-layer ("medallion") architecture:

```bash
raw files (CSV/XML/zip)        bronze                  silver
------------------------  -->  --------------------  -->  --------------------
On disk, agency-supplied      Typed, validated,           Per-source, normalized
formats, one file per day/    Hive-partitioned Parquet    PostgreSQL+PostGIS
export/snapshot                (data/bronze/...)          tables (schema `silver`)
```

- **Bronze**: raw agency exports (CSV, nested XML, zipped GTFS feeds) are
  parsed, validated against a [Pandera](https://pandera.readthedocs.io/)
  schema, and written as Hive-partitioned Parquet. No cross-source logic,
  no joins, minimal reshaping. This layer exists to turn "whatever format
  the agency happened to ship" into a uniform, typed, columnar one.
- **Silver**: each bronze source is loaded into its own normalized table in
  PostgreSQL+PostGIS (schema `silver`). Still strictly per-source (no
  AFC-to-GPS vehicle reconciliation, no GTFS-to-ridership joins here) but
  now typed, deduplicated, indexed, and queryable with SQL/PostGIS.

See [`architecture.md`](architecture.md) for the full design
rationale.

## Data sources

| Source | What it is | Cadence | Bronze partition key |
| --- | --- | --- | --- |
| **AVL/GPS** | Vehicle position pings (lat/lon, speed, odometer, route) | Continuous, files per day | `year`/`month`/`day` of the ping |
| **AFC** (bilhetagem) | Fare-collection boarding events, nested XML | Delayed-upload backlog, one dump file per day | `year`/`month`/`day` of the **dump** (not the ridership date) |
| **GTFS** | Scheduled routes, trips, stops, calendars, fares | One zipped feed export whenever the schedule changes | `year`/`month`/`day` of the **export** |
| **Vehicle dictionary** | AFC vehicle code (`cod_veiculo`) to GPS vehicle id (`id_veiculo`) mapping | A single live reference file, snapshotted on ingest | `year`/`month`/`day` of the snapshot |

Full details on each source's quirks (timezones, delayed uploads, filename
conventions, known data-quality issues) are in
[`architecture.md`](architecture.md).

## Repository layout

```text
src/opa_database/
    config.py            Settings (env-driven: raw_data_root, bronze_root, db_dsn)
    cli.py                Click CLI: ingest, ingest-reference, load-silver, load-silver-reference
    contracts/            Pandera schemas for each raw source (bronze validation)
    adapters/             Raw file -> validated bronze Parquet, one module per source
    loaders/
        bronze.py         Generic Hive-partitioned Parquet writer
        silver.py         Generic idempotent "replace a period" loader for silver tables
    silver/                Bronze Parquet -> silver PostgreSQL+PostGIS, one module per source

docs/                      Architecture reference, remote access
docker-compose.yml         Postgres+PostGIS and Adminer (web SQL UI)
```

## Getting started

### Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker + Docker Compose (for the Postgres+PostGIS silver database)

### Setup

```bash
uv sync
cp .env.example .env   # then edit RAW_DATA_ROOT to point at your raw data
docker compose up -d   # starts Postgres+PostGIS on :5432 and Adminer on :8080
```

`.env` variables (see `.env.example`):

| Variable | Purpose |
| --- | --- |
| `RAW_DATA_ROOT` | Path to the raw agency data on disk |
| `BRONZE_ROOT` | Where bronze Parquet files are written |
| `DB_USER` / `DB_PASSWORD` / `DB_NAME` | Postgres credentials, used both by `docker compose` and by the app |
| `DB_DSN` | Full connection string the pipeline uses to reach Postgres. Shared by every schema (`silver`, `ml`, ...), not silver-specific |
| `BIND_HOST` | Optional. Network interface Postgres/Adminer bind to, defaults to `127.0.0.1` (localhost-only). See [`remote-access.md`](remote-access.md) to expose them over Tailscale instead |

### Running the pipeline

```bash
# Bronze: ingest a raw source for a given month
uv run opa-database ingest avl --year 2023 --month 11
uv run opa-database ingest afc --year 2023 --month 11
uv run opa-database ingest gtfs --year 2023 --month 11
uv run opa-database ingest-reference vehicle_dictionary

# Silver: load that month's bronze data into PostgreSQL+PostGIS
uv run opa-database load-silver avl --year 2023 --month 11
uv run opa-database load-silver afc --year 2023 --month 11
uv run opa-database load-silver gtfs --year 2023 --month 11
uv run opa-database load-silver-reference vehicle_dictionary
```

Each `load-silver`/`load-silver-reference` run is idempotent: re-running it
for the same period deletes and reloads just that period, rather than
duplicating rows.

### Accessing the database

The `docker-compose.yml` Postgres instance is reachable directly
(`psql`, DBeaver, TablePlus, etc.) or through the bundled Adminer web UI at
`http://localhost:8080`. For access from another machine over Tailscale,
see [`remote-access.md`](remote-access.md).
