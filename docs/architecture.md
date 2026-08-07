# Architecture

OPA Database ingests four raw sources describing Fortaleza's public transit
system and moves them through two layers: **bronze** (typed raw data) and
**silver** (per-source normalized SQL tables). Each layer has a different
job and deliberately does not do the next layer's work.

## Why two layers

- **Bronze** exists to absorb the raw data's format problems once:
  inconsistent folder naming, headerless CSVs, deeply nested XML, GTFS's
  zero-padded string ids, delayed/backlogged uploads. Nothing downstream
  should ever need to know these things existed.
- **Silver** exists to make each source queryable on its own: typed
  columns, PostGIS geometries, indexes, deduplication, and timezone
  correctness. It stays flat and per-source on purpose (see below): no
  cross-source joins, no denormalization removal.

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
- **GTFS** (`adapters/gtfs.py`): the `exportacao_YYYY-MM-DD.zip` naming
  convention (used since 2020, 72 canonical exports) plus three 2015-2019
  legacy naming schemes (`exportacaoDDMMYYYY.zip`, an optional stray space
  before the date, and `exportacao_DD-MM-YYYY.zip`) are all supported —
  `_EXPORT_NAME_PATTERNS` tries each in turn. One legacy filename has a
  data-entry typo in the year (`2818` for `2018`), corrected explicitly via
  `_FILENAME_YEAR_CORRECTIONS`. GTFS ids (`route_id`, `stop_id`, etc.)
  sometimes carry meaningful leading zeros, so raw CSVs are read with
  `infer_schema_length=0` (every column as a string) before Pandera
  coerces each into its declared type; letting Polars infer types itself
  would silently strip those zeros. A handful of exports (five of the 72
  canonical ones, plus several legacy ones) are missing an entire raw
  table file inside their zip (`calendar_dates.txt` or `stop_times.txt` —
  a verified one-off data-quality issue, not an ongoing pattern). For just
  these two tables, `ingest()` falls back to the nearest other export (by date,
  ties preferring the earlier export) that actually has the file, and
  tags every borrowed row with a non-null `copied_from_feed_version_date`
  column holding that export's date; every other row of these two tables
  carries `null`. Unlike this section's other raw-format quirks, this one
  is deliberately *not* made fully invisible: the fact that data was
  borrowed is preserved as a real column through silver
  (`copied_from_feed_version_date`), not swallowed at bronze.
- **Vehicle dictionary** (`adapters/vehicle_dictionary.py`): maps AFC's
  `cod_veiculo` to GPS's `id_veiculo`. `cod_veiculo` is not a reliable
  unique key even within one snapshot: buses get reassigned, so ~2% of
  codes map to more than one `id_veiculo`. Bronze keeps this as-is;
  reconciling which mapping is current is left to downstream consumers.

## Silver layer

Location: `src/opa_database/silver/`, `src/opa_database/loaders/silver.py`.
Backing store: PostgreSQL 16 + PostGIS 3.4 (`docker-compose.yml`), schema
`silver`.

Every silver table is a native Postgres `PARTITION BY RANGE` parent, one
partition per load period (monthly for AVL/AFC, daily for GTFS/vehicle
dictionary — matching each source's own load granularity exactly). Every
silver loader follows the same **idempotent "replace a period"** pattern,
implemented once in `loaders/silver.py::replace_period`:

1. Bootstrap the parent table (`CREATE TABLE IF NOT EXISTS ... PARTITION
   BY RANGE (...)`) if it doesn't exist.
2. Inside one transaction: `DROP TABLE IF EXISTS` the target partition
   (if this period was already loaded — this removes its rows *and* its
   indexes in one metadata operation), `CREATE TABLE ... PARTITION OF
   ... FOR VALUES FROM (...) TO (...)` a fresh one, bulk-load the new
   data via `COPY` (a single vectorized CSV write, not a Python
   per-row loop), then create that partition's indexes.

Indexes are created after the bulk load rather than maintained
incrementally during the `COPY`, since incremental index maintenance
(especially the GiST spatial index) is dramatically slower than one batch
build for a multi-million-row load; this is Postgres's own documented
recommendation. Scoping the drop/rebuild to a single partition (rather
than the whole table, as an earlier version of this design did) means
that cost scales with one period's size, not the table's total
accumulated history — reloading any one month/day costs the same
regardless of how many other months/days already exist.

This makes "reload November" a safe, repeatable operation: run it twice
and you get the same rows, not duplicates.

Each silver table's period key matches its bronze partition key
(`metric_timestamp` bucket for AVL, `dump_date` for AFC, `feed_version_date`
for GTFS, `snapshot_date` for the vehicle dictionary): silver reprocesses
"the same period bronze uses," not a recomputed one.

One accepted tradeoff from partitioning: AFC's `event_id` unique index
used to guarantee uniqueness across the table's entire history (a single
unpartitioned index); it's now per-partition (per-month), so it only
catches a duplicate within the same month. A duplicate `event_id` landing
in two different months would no longer be caught automatically. Postgres
has no native way to enforce true cross-partition uniqueness on a
non-partition-key column; the empirical finding backing this index (zero
overlap across all 435 day-pairs in November 2023) still stands as
evidence about the real data, this only weakens the automatic safety net
for a hypothetical future violation.

Silver stays **flat and per-source on purpose**. For example,
`silver.afc_boardings` repeats every trip/line/vehicle/company attribute on
every boarding row (measured ~16.4x redundancy) instead of being split into
a trip/boarding fact-dimension pair. Keeping silver a thin,
obviously-correct typed mirror of the raw data makes it a stable
foundation for any downstream consumer to build on.

### Timezone handling

AFC's raw timestamps are naive Fortaleza local time (UTC-3, no DST since
2008); AVL/GPS's are already UTC (per the raw feed's own field dictionary).
Both are localized/converted to proper `timestamptz` values during the
silver load (`silver/afc.py`, `silver/avl.py`): a relabeling for AVL and
a real timezone conversion for AFC. Reconciling AFC and AVL events
against each other happens at silver time (each is independently correct
in UTC), never in bronze.

### PostGIS geometry columns

Where a source carries lat/lon, the silver table adds a `geometry(Point,
4326)` column as a Postgres `GENERATED ALWAYS AS ... STORED` column (see
`silver/avl.py`, `silver/afc.py`, `silver/gtfs.py`) rather than being
computed in Python and inserted directly. This means the bulk `COPY` only
ever writes the plain lat/lon columns; Postgres computes and indexes the
geometry itself, so there's no staging-table step needed to populate a
generated column via `COPY`.

## Known gaps

- No cross-source fact table yet joins AFC + AVL + GTFS together in one
  place (each pairwise piece exists independently).
- No CI job runs the bronze/silver pipeline end-to-end (would need a
  Postgres+PostGIS service container and sample data in GitHub Actions);
  CI currently only lints, type-checks, and runs the (currently empty)
  Python test suite.
- Pre-2020 AFC and GTFS raw formats aren't supported by their adapters.
