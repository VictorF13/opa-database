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

uv run prek run --all-files   # dry-run every pre-commit hook before committing
```

There's no single-test invocation documented yet since the pytest suite is
empty; use standard `pytest path::test_name` once tests exist.

**Before considering any code change done** (this includes notebooks),
run `uv run prek run --all-files` and fix everything it reports — don't
stop at a clean `ruff check` in isolation; `prek` also runs `ruff
format`, `ty check`, and the `requirements*.txt`/`uv.lock` sync hooks.
Two gotchas learned the hard way:

- `prek run --all-files` only checks files **git already tracks**. A
  freshly created, still-untracked file (e.g. a new notebook) is
  silently skipped — `git add` it first, or run `ruff
  check`/`ruff format --check`/`ty check` directly against the new
  path(s), before trusting a clean `prek` result.
- The `ruff-check`/`ruff-format` hooks cover `.ipynb` files, not just
  `.py` — don't assume notebooks are exempt from linting.

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

## Architecture

This is a two-layer ("medallion") pipeline turning Fortaleza, Brazil's
raw public transit data into a queryable PostgreSQL+PostGIS database:
**bronze** (raw files -> typed Parquet) -> **silver** (Parquet -> per-source
normalized Postgres tables). Full rationale in `docs/architecture.md`; the
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
unfinished.

Timezones: AFC's raw timestamps are naive Fortaleza local time (UTC-3, no
DST since 2008) and are converted to UTC during the silver load; AVL/GPS is
already UTC and is just relabeled `timestamptz`. Don't assume both sources'
raw timestamps mean the same thing before this conversion.

PostGIS `geometry(Point, 4326)` columns are Postgres `GENERATED ALWAYS AS
... STORED` columns computed from lat/lon by Postgres itself, not written
directly; bulk `COPY` only ever carries the plain lat/lon columns.

## CI / release

`.github/workflows/ci.yml`: PR titles are enforced as Conventional Commits;
`validate` runs ruff format/check + `ty check`; `test` runs pytest
(tolerates zero tests collected). Releases are fully automated by
python-semantic-release off `develop`/`main`: version bumps, changelog,
tags, and GitHub releases are generated from commit history, never hand-edited.
