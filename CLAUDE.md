# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                          # install/sync dependencies (Python 3.12 required, see below)
docker compose up -d             # start Postgres+PostGIS (:5432) and Adminer (:8080)

uv run ruff format --check .     # formatting
uv run ruff check .              # linting
uv run ty check                  # type checking
uv run pytest                    # tests (currently no test suite yet; CI tolerates exit code 5)

# Bronze/silver pipeline (Click CLI, entry point `opa-database`)
uv run opa-database ingest {avl,afc,gtfs} --year Y --month M
uv run opa-database ingest-reference vehicle_dictionary
uv run opa-database load-silver {avl,afc,gtfs} --year Y --month M
uv run opa-database load-silver-reference vehicle_dictionary

# Gold layer (dbt project lives in gold/, not under src/)
uv run dbt run  --project-dir gold --profiles-dir gold
uv run dbt test --project-dir gold --profiles-dir gold

uv run prek run --all-files   # dry-run every pre-commit hook before committing
```

There's no single-test invocation documented yet since the pytest suite is
empty; use standard `pytest path::test_name` once tests exist.

Always invoke Python through `uv run` (`uv run <script.py>`, `uv run
pytest`, ...) rather than calling `python`/`python3` directly, so it runs
against this project's synced environment and pinned Python version.
Manage dependencies with `uv add "pkg"` / `uv add --dev "pkg"` /
`uv remove pkg`, never by hand-editing `pyproject.toml` (that also keeps
`uv.lock` in sync; `requirements.txt`/`requirements-dev.txt` are
regenerated from it by pre-commit, not edited directly).

Every public module/class/function needs a Google-style docstring
(imperative summary line, then `Args:`/`Returns:`/`Raises:` sections as
needed). See `src/opa_database/config.py` or
`src/opa_database/loaders/silver.py::replace_period` for the existing
convention to match.

**Python version is pinned to 3.12** (`pyproject.toml`, `.python-version`).
This is a hard requirement, not a preference. `dbt-core`'s dependency
`mashumaro` fails to import under Python 3.14 (`UnserializableField` on a
plain `Optional[str]`, verified across the entire mashumaro version range
dbt allows) and hits a different incompatibility under 3.13. If dbt starts
throwing `mashumaro`/`typing_extensions` errors, check whether 3.13/3.14
crept back in before assuming it's a config problem. Don't try pinning
mashumaro/typing_extensions instead, that was already tried and failed.

## Architecture

This is a three-layer ("medallion") pipeline turning Fortaleza, Brazil's
raw public transit data into a queryable PostgreSQL+PostGIS database:
**bronze** (raw files -> typed Parquet) -> **silver** (Parquet -> per-source
normalized Postgres tables) -> **gold** (dbt models joining across sources).
Full rationale in `docs/architecture.md` and `docs/gold-layer.md`; the
essential cross-file structure is:

### Bronze (`src/opa_database/{adapters,contracts}/`, `loaders/bronze.py`)

One **adapter** + one **contract** (Pandera `DataFrameModel`) per source
(`avl`, `afc`, `gtfs`, `vehicle_dictionary`). The adapter finds/parses raw
files, validates against the contract, and calls the shared
`loaders/bronze.py::write_bronze` to emit one Hive-partitioned Parquet file
per invocation. **Each source partitions by a different notion of "period"**:
this is the single most important thing to know before touching bronze.

- AVL: day the GPS ping occurred.
- AFC: day the **dump file** arrived, not the ridership `service_date`.
  Dumps are a delayed-upload backlog (validators buffer offline and upload
  late), so one dump can span weeks of `service_date`s. `event_id` is
  globally unique across dumps, so this is late delivery, not
  resend/correction.
- GTFS: day the feed **export** happened (`feed_version_date`), a whole
  snapshot, not a calendar period.
- Vehicle dictionary: day it was **snapshotted**. It's a live reference
  file, not a time series.

### Silver (`src/opa_database/silver/`, `loaders/silver.py`)

One loader module per source, all built on the single shared primitive
`loaders/silver.py::replace_period`: bootstrap the table DDL, then inside
one transaction, drop indexes -> `DELETE` the target period -> bulk `COPY`
-> recreate indexes. This is what makes reloading a period idempotent
(delete-then-insert, not append). Every source's silver period key matches
its bronze partition key.

Silver is **strictly per-source and stays flat/denormalized** (e.g.
`silver.afc_boardings` repeats every trip/line/vehicle/company column on
every boarding row, ~16.4x redundancy, measured). This is deliberate, not
unfinished. Fact/dimension splitting and any cross-source join belongs in
gold, not silver.

Timezones: AFC's raw timestamps are naive Fortaleza local time (UTC-3, no
DST since 2008) and are converted to UTC during the silver load; AVL/GPS is
already UTC and is just relabeled `timestamptz`. Don't assume both sources'
raw timestamps mean the same thing before this conversion.

PostGIS `geometry(Point, 4326)` columns are Postgres `GENERATED ALWAYS AS
... STORED` columns computed from lat/lon by Postgres itself, not written
directly; bulk `COPY` only ever carries the plain lat/lon columns.

### Gold (`gold/`, a self-contained dbt-core project)

Not under `src/opa_database/`, a separate SQL toolchain. Builds on
`silver` via `{{ source(...) }}`, materializes into schema `gold`. Full
model inventory and testing conventions in `docs/gold-layer.md`; key
patterns that apply to any new gold model:

- **GTFS/AFC ids repeat across snapshots**, so every model's real key is a
  composite `(feed_version_date, entity_id)` (or the AFC equivalent), never
  a bare id. dbt's built-in single-column `relationships`/`unique` tests
  would silently pass broken references in this shape, so referential
  integrity and uniqueness are enforced with hand-written singular tests in
  `gold/tests/*_exists.sql` / `*_unique_per_*.sql` that join/group on the
  full composite key.
- **Surrogate keys** for entities with no natural id (AFC has no trip id)
  use `md5(concat_ws('|', ...))` over the full natural-key column set,
  factored into a shared macro (`gold/macros/afc_trip_key.sql`) rather than
  repeated inline.
- **Conformed dimension pattern**: `dim_vehicle_master` reconciles AFC
  vehicle identity (`vehicle_number`) and GPS vehicle identity
  (`vehicle_id`), which are independently assigned by each source. It's a
  deterministic function of the latest `vehicle_dictionary` snapshot (not
  a persisted/stateful registry). Every vehicle from every source gets
  exactly one `master_vehicle_id`, synthesizing a source-prefixed id where
  no confirmed match exists, tagged via `match_status` rather than dropped.
- **Don't duplicate an id reachable through an existing relationship**:
  fact tables carry `master_vehicle_id` only at the grain it canonically
  belongs to (e.g. `dim_afc_trip`, not `fact_afc_boarding`), and never keep
  a raw per-source id alongside the conformed one once it's recoverable via
  a dimension's crosswalk column.
- Referential integrity in gold is dbt tests, not enforced Postgres
  `FOREIGN KEY`/`PRIMARY KEY` constraints (dbt model contracts aren't
  turned on yet).

## CI / release

`.github/workflows/ci.yml`: PR titles are enforced as Conventional Commits;
`validate` runs ruff format/check + `ty check`; `test` runs pytest
(tolerates zero tests collected). Releases are fully automated by
python-semantic-release off `develop`/`main`: version bumps, changelog,
tags, and GitHub releases are generated from commit history, never hand-edited.
