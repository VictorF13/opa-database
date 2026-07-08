# Architecture

OPA Database ingests four raw sources describing Fortaleza's public transit
system and moves them through three layers: **bronze** (typed raw data),
**silver** (per-source normalized SQL tables), and **gold** (cross-source
dimensional models). Each layer has a different job and deliberately does
not do the next layer's work.

## Why three layers

- **Bronze** exists to absorb the raw data's format problems once:
  inconsistent folder naming, headerless CSVs, deeply nested XML, GTFS's
  zero-padded string ids, delayed/backlogged uploads. Nothing downstream
  should ever need to know these things existed.
- **Silver** exists to make each source queryable on its own: typed
  columns, PostGIS geometries, indexes, deduplication, and timezone
  correctness. It stays flat and per-source on purpose (see below): no
  cross-source joins, no denormalization removal.
- **Gold** exists for everything that requires combining or reshaping
  data: fact/dimension splits, surrogate keys, cross-source identity
  resolution, and schedule-validity logic. It's a dbt project because that
  kind of modeling benefits from being expressed, tested, and iterated on
  as SQL rather than as pipeline code.

## Bronze layer

Location: `src/opa_database/adapters/`, `src/opa_database/contracts/`,
`src/opa_database/loaders/bronze.py`.

Each source has an **adapter** (`adapters/<source>.py`) that knows how to
find and parse that source's raw files, and a **contract**
(`contracts/<source>.py`), a [Pandera](https://pandera.readthedocs.io/)
`DataFrameModel` every row is validated against before being written. The
generic writer (`loaders/bronze.py`) writes a DataFrame as a single
Hive-partitioned Parquet file per invocation
(`<bronze_root>/<source>/year=Y/month=M/day=D/data.parquet`).

Bronze's partition key is **the source's own natural unit of delivery**,
not always "the day the event happened":

| Source | Partitioned by | Why |
| --- | --- | --- |
| AVL/GPS | Day the ping occurred | One CSV file per day, already the natural grain |
| AFC | Day the **dump file** arrived | A dump is a delayed-upload backlog: validators without connectivity buffer transactions locally and upload whenever they reconnect, so one dump can contain `service_date`s going back weeks. `event_id` is globally unique across dumps (verified: zero overlap across all 435 day-pairs in November 2023), so this isn't a resend/correction system, just late delivery. Partitioning by dump date (not `service_date`) matches how the data actually arrives; `service_date` stays as a normal column. |
| GTFS | Day the feed **export** happened | A schedule snapshot stays in effect until the next export supersedes it; there's no "November's schedule" the way there's "November's ridership." Every table in one export shares the same `feed_version_date`. |
| Vehicle dictionary | Day it was **snapshotted** | It's a single live reference file (not a time series of raw files), updated in place. Each ingestion run snapshots it as of a given date so historical mappings aren't lost when the file changes. |

Source-specific notes worth knowing before touching an adapter:

- **AVL/GPS** (`adapters/avl.py`): raw folder names are inconsistent
  Portuguese month names (`"NOVEMBRO-2023"`, `"ABRIL - 2023"`); matched by
  normalizing (strip accents/digits/punctuation, uppercase) rather than
  literal string match. Pings just after midnight are held back and merged
  into the next file's data so a day's data isn't split across two writes.
- **AFC** (`adapters/afc.py`): the raw feed is deeply nested XML
  (`Movimentos > MovimentoDiario > Categoria > Empresa > Veiculo > Linha >
  Viagem > Passageiro`). The adapter streams it with `iterparse`, flattening
  every ancestor's attributes onto each `Passageiro` (boarding event) row,
  clearing elements as it goes to keep memory bounded on ~15M rows/month.
  Only the `V{YYYYMMDD}.zip` filename convention (used since 2020) is
  supported; pre-2020 formats are out of scope for now.
- **GTFS** (`adapters/gtfs.py`): only the `exportacao_YYYY-MM-DD.zip`
  naming convention (used since 2020) is supported. GTFS ids (`route_id`,
  `stop_id`, etc.) sometimes carry meaningful leading zeros, so raw CSVs
  are read with `infer_schema_length=0` (every column as a string) before
  Pandera coerces each into its declared type; letting Polars infer types
  itself would silently strip those zeros.
- **Vehicle dictionary** (`adapters/vehicle_dictionary.py`): maps AFC's
  `cod_veiculo` to GPS's `id_veiculo`. `cod_veiculo` is not a reliable
  unique key even within one snapshot: buses get reassigned, so ~2% of
  codes map to more than one `id_veiculo`. Bronze keeps this as-is;
  reconciling which mapping is current is deferred to gold
  (`dim_vehicle_master`, see [`gold-layer.md`](gold-layer.md)).

## Silver layer

Location: `src/opa_database/silver/`, `src/opa_database/loaders/silver.py`.
Backing store: PostgreSQL 16 + PostGIS 3.4 (`docker-compose.yml`), schema
`silver`.

Every silver loader follows the same **idempotent "replace a period"**
pattern, implemented once in `loaders/silver.py::replace_period`:

1. Bootstrap the table (`CREATE TABLE IF NOT EXISTS`) if it doesn't exist.
2. Inside one transaction: drop the table's indexes, `DELETE` any existing
   rows in `[start, end)` of the period being loaded, bulk-load the new
   data via `COPY` (a single vectorized CSV write, not a Python
   per-row loop), then recreate the indexes.

Indexes are dropped and rebuilt in bulk rather than maintained
incrementally during the `COPY`, since incremental index maintenance
(especially the GiST spatial index) is dramatically slower than one batch
rebuild for a multi-million-row load; this is Postgres's own documented
recommendation. The tradeoff: every load rebuilds indexes for the *whole*
table, not just the period changed, so cost scales with total table size.
Fine for the current handful of months of history; worth revisiting (e.g.
native monthly partitioning) if it stops being fine.

This makes "reload November" a safe, repeatable operation: run it twice
and you get the same rows, not duplicates.

Each silver table's period key matches its bronze partition key
(`metric_timestamp` bucket for AVL, `dump_date` for AFC, `feed_version_date`
for GTFS, `snapshot_date` for the vehicle dictionary): silver reprocesses
"the same period bronze uses," not a recomputed one.

Silver stays **flat and per-source on purpose**. For example,
`silver.afc_boardings` repeats every trip/line/vehicle/company attribute on
every boarding row (measured ~16.4x redundancy) instead of being split into
a trip/boarding fact-dimension pair. That split, along with any
cross-source join (AFC vehicle identity vs. GPS vehicle identity, GTFS
schedule validity, etc.), is gold's job. Dimensional modeling is much
easier to iterate on as dbt SQL than as pipeline Python, and keeping silver
a thin, obviously-correct typed mirror of the raw data makes it a stable
foundation to model on top of.

### Timezone handling

AFC's raw timestamps are naive Fortaleza local time (UTC-3, no DST since
2008); AVL/GPS's are already UTC (per the raw feed's own field dictionary).
Both are localized/converted to proper `timestamptz` values during the
silver load (`silver/afc.py`, `silver/avl.py`): a relabeling for AVL and
a real timezone conversion for AFC. Reconciling AFC and AVL events
against each other happens at silver time (each is independently correct
in UTC) or later in gold, never in bronze.

### PostGIS geometry columns

Where a source carries lat/lon, the silver table adds a `geometry(Point,
4326)` column as a Postgres `GENERATED ALWAYS AS ... STORED` column (see
`silver/avl.py`, `silver/afc.py`, `silver/gtfs.py`) rather than being
computed in Python and inserted directly. This means the bulk `COPY` only
ever writes the plain lat/lon columns; Postgres computes and indexes the
geometry itself, so there's no staging-table step needed to populate a
generated column via `COPY`.

## Gold layer

Location: `gold/` (a self-contained dbt project). See
[`gold-layer.md`](gold-layer.md) for the full model inventory, testing
conventions, and how to run it. In short: gold is where the fact/dimension
split of AFC happens, where a conformed vehicle dimension unifies AFC and
GPS vehicle identities, and where every GTFS table gets a gold-layer
counterpart so nothing needs to fall back to silver directly.

## Known gaps

- No fact table yet joins AFC + AVL + GTFS together in one place (each
  pairwise piece exists independently).
- Referential integrity in gold is enforced via dbt tests, not actual
  Postgres `FOREIGN KEY`/`PRIMARY KEY` constraints. dbt supports enforced
  DDL-level constraints via "model contracts," not yet turned on.
- No CI job runs the bronze/silver/gold pipeline end-to-end (would need a
  Postgres+PostGIS service container and sample data in GitHub Actions);
  CI currently only lints, type-checks, and runs the (currently empty)
  Python test suite.
- Pre-2020 AFC and GTFS raw formats aren't supported by their adapters.
