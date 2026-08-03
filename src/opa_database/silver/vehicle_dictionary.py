"""Silver loaders for the vehicle dictionary family of bronze sources.

All six sources under `bronze/{vehicle_dictionary,device_dictionary,
vehicle_dictionary_legacy,vehicle_dictionary_legacy2,
vehicle_dictionary_2018,vehicle_dictionary_antigo}` load from this one
module, mirroring `adapters/vehicle_dictionary.py`. Bronze source (and
CLI source-argument) names are unchanged; only the silver table names
carry a `dictionary_` prefix instead (`dictionary_vehicle`,
`dictionary_device`, `dictionary_legacy`, `dictionary_legacy2`,
`dictionary_2018`, `dictionary_antigo`), so they sort together
alphabetically the same way `avl_*`/`afc_*`/`gtfs_*` do. Every one of
them is kept as its own silver table rather than merged into
`silver.dictionary_vehicle`, same reasoning as bronze: none of these
files' id spaces are confirmed compatible with the live file's.

Every table here follows the same shape: partitioned by day (matching
each loader's own single-snapshot load calls, see
`loaders/silver.py::replace_period`), with a unique index on whichever
column is confirmed unique within a snapshot (verified against each
bronze source's own data) as a data-integrity safeguard, and a plain
index on the other side of the mapping for lookup. Row counts are all in
the low thousands, so partitioning here is about consistency with the
other silver tables rather than a real perf need -- same note as the
original `dictionary_vehicle` table.
"""

from __future__ import annotations

import datetime
from typing import LiteralString

import polars as pl

from opa_database.config import settings
from opa_database.loaders.silver import (
    IndexSpec,
    daily_partition_name,
    get_connection,
    replace_period,
)

_VEHICLE_DICTIONARY_TABLE = "silver.dictionary_vehicle"
_LEGACY_TABLE = "silver.dictionary_legacy"
_LEGACY2_TABLE = "silver.dictionary_legacy2"
_DEVICE_DICTIONARY_TABLE = "silver.dictionary_device"
_2018_TABLE = "silver.dictionary_2018"
_ANTIGO_TABLE = "silver.dictionary_antigo"

_VEHICLE_DICTIONARY_PARENT_DDL = """
CREATE TABLE IF NOT EXISTS silver.dictionary_vehicle (
    snapshot_date date NOT NULL,
    cod_veiculo text NOT NULL,
    id_veiculo text NOT NULL
) PARTITION BY RANGE (snapshot_date);
"""

_LEGACY_PARENT_DDL = """
CREATE TABLE IF NOT EXISTS silver.dictionary_legacy (
    snapshot_date date NOT NULL,
    vehicleid text NOT NULL,
    numbus text NOT NULL
) PARTITION BY RANGE (snapshot_date);
"""

_LEGACY2_PARENT_DDL = """
CREATE TABLE IF NOT EXISTS silver.dictionary_legacy2 (
    snapshot_date date NOT NULL,
    id text NOT NULL,
    carro text NOT NULL,
    obs1 text,
    obs2 text
) PARTITION BY RANGE (snapshot_date);
"""

_DEVICE_DICTIONARY_PARENT_DDL = """
CREATE TABLE IF NOT EXISTS silver.dictionary_device (
    snapshot_date date NOT NULL,
    codigo text NOT NULL,
    device_id text,
    vehicle_type text NOT NULL,
    company text NOT NULL,
    plate text,
    vehicle_number text NOT NULL,
    status text NOT NULL,
    action text
) PARTITION BY RANGE (snapshot_date);
"""

_2018_PARENT_DDL = """
CREATE TABLE IF NOT EXISTS silver.dictionary_2018 (
    snapshot_date date NOT NULL,
    cod_veiculo text NOT NULL,
    id_veiculo text NOT NULL
) PARTITION BY RANGE (snapshot_date);
"""

_ANTIGO_PARENT_DDL = """
CREATE TABLE IF NOT EXISTS silver.dictionary_antigo (
    snapshot_date date NOT NULL,
    cod_veiculo text NOT NULL,
    id_veiculo text NOT NULL
) PARTITION BY RANGE (snapshot_date);
"""

# cod_veiculo/vehicleid/carro/codigo (the AFC/"business" side of each
# mapping) are deliberately not unique, even within one snapshot, in
# general -- dictionary_vehicle's own cod_veiculo is the proven case
# (~2% reassigned). The GPS/device side (id_veiculo, vehicleid's
# counterpart numbus... see below) gets the real unique constraint as a
# data-integrity safeguard instead, verified unique within a snapshot for
# every one of these sources against their first extraction.
_VEHICLE_DICTIONARY_INDEXES = (
    IndexSpec("snapshot_date_idx", unique=False, definition="(snapshot_date)"),
    IndexSpec("cod_veiculo_idx", unique=False, definition="(cod_veiculo)"),
    IndexSpec("snapshot_id_key", unique=True, definition="(snapshot_date, id_veiculo)"),
)

# Unlike dictionary_vehicle's cod_veiculo/id_veiculo, both sides of this
# particular mapping were verified unique within a snapshot (0 duplicates
# on either column, first extraction) -- indexed accordingly, unique on
# vehicleid (the GPS-side id, for parity with the other tables here) and
# plain on numbus for lookup from the other direction.
_LEGACY_INDEXES = (
    IndexSpec("snapshot_date_idx", unique=False, definition="(snapshot_date)"),
    IndexSpec("numbus_idx", unique=False, definition="(numbus)"),
    IndexSpec(
        "snapshot_vehicleid_key", unique=True, definition="(snapshot_date, vehicleid)"
    ),
)

# Both id and carro were verified unique within a snapshot (0 duplicates
# on either column, first extraction).
_LEGACY2_INDEXES = (
    IndexSpec("snapshot_date_idx", unique=False, definition="(snapshot_date)"),
    IndexSpec("carro_idx", unique=False, definition="(carro)"),
    IndexSpec("snapshot_id_key", unique=True, definition="(snapshot_date, id)"),
)

# codigo (always populated) and device_id (nullable) were both verified
# unique among their non-null values within a snapshot, so both get a
# real unique constraint -- Postgres unique indexes allow any number of
# NULLs, so device_id's nullability doesn't weaken it. vehicle_number is
# the more likely join target for future AVL/AFC matching work, so it
# gets the plain lookup index even though it was also confirmed unique.
_DEVICE_DICTIONARY_INDEXES = (
    IndexSpec("snapshot_date_idx", unique=False, definition="(snapshot_date)"),
    IndexSpec("vehicle_number_idx", unique=False, definition="(vehicle_number)"),
    IndexSpec("snapshot_codigo_key", unique=True, definition="(snapshot_date, codigo)"),
    IndexSpec(
        "snapshot_device_id_key", unique=True, definition="(snapshot_date, device_id)"
    ),
)

_VEHICLE_DICTIONARY_COLUMNS = ("cod_veiculo", "id_veiculo")
_LEGACY_COLUMNS = ("vehicleid", "numbus")
_LEGACY2_COLUMNS = ("id", "carro", "obs1", "obs2")
_DEVICE_DICTIONARY_COLUMNS = (
    "codigo",
    "device_id",
    "vehicle_type",
    "company",
    "plate",
    "vehicle_number",
    "status",
    "action",
)


def _find_latest_snapshot(source: str) -> datetime.date:
    root = settings.bronze_root / source
    dates = [
        datetime.date(
            int(year_dir.name.removeprefix("year=")),
            int(month_dir.name.removeprefix("month=")),
            int(day_dir.name.removeprefix("day=")),
        )
        for year_dir in root.glob("year=*")
        for month_dir in year_dir.glob("month=*")
        for day_dir in month_dir.glob("day=*")
    ]
    if not dates:
        msg = f"No {source} bronze snapshots found under {root}"
        raise FileNotFoundError(msg)
    return max(dates)


def _bronze_path(source: str, date: datetime.date) -> str:
    return str(
        settings.bronze_root
        / source
        / f"year={date.year}"
        / f"month={date.month}"
        / f"day={date.day}"
        / "data.parquet"
    )


def _load_snapshot(
    *,
    source: str,
    table: str,
    columns: tuple[str, ...],
    parent_ddl: LiteralString,
    indexes: tuple[IndexSpec, ...],
    snapshot_date: datetime.date | None,
) -> str:
    date = snapshot_date or _find_latest_snapshot(source)
    df = (
        pl.scan_parquet(_bronze_path(source, date))
        .with_columns(pl.lit(date).alias("snapshot_date"))
        .select("snapshot_date", *columns)
        .collect()
    )

    partition = daily_partition_name(table.split(".", maxsplit=1)[1], date)
    with get_connection() as conn:
        replace_period(
            conn,
            table,
            partition,
            df,
            partition_start=date,
            partition_end=date + datetime.timedelta(days=1),
            parent_ddl=parent_ddl,
            indexes=indexes,
        )
    return table


def load(snapshot_date: datetime.date | None = None) -> str:
    """Load a vehicle_dictionary bronze snapshot into the silver layer.

    Defaults to the most recent bronze snapshot available. Keyed by its
    own ingestion date (`snapshot_date`) rather than a calendar period —
    same "period = bronze's own partition key" pattern as AFC's dump_date
    and GTFS's feed_version_date.

    Args:
        snapshot_date (datetime.date | None): Bronze snapshot date to
            load. Defaults to the most recent snapshot found under
            `bronze_root`.

    Returns:
        str: The fully-qualified silver table loaded into.

    """
    return _load_snapshot(
        source="vehicle_dictionary",
        table=_VEHICLE_DICTIONARY_TABLE,
        columns=_VEHICLE_DICTIONARY_COLUMNS,
        parent_ddl=_VEHICLE_DICTIONARY_PARENT_DDL,
        indexes=_VEHICLE_DICTIONARY_INDEXES,
        snapshot_date=snapshot_date,
    )


def load_legacy(snapshot_date: datetime.date | None = None) -> str:
    """Load a vehicle_dictionary_legacy bronze snapshot into the silver layer.

    Args:
        snapshot_date (datetime.date | None): Bronze snapshot date to
            load. Defaults to the most recent snapshot found under
            `bronze_root`.

    Returns:
        str: The fully-qualified silver table loaded into.

    """
    return _load_snapshot(
        source="vehicle_dictionary_legacy",
        table=_LEGACY_TABLE,
        columns=_LEGACY_COLUMNS,
        parent_ddl=_LEGACY_PARENT_DDL,
        indexes=_LEGACY_INDEXES,
        snapshot_date=snapshot_date,
    )


def load_legacy2(snapshot_date: datetime.date | None = None) -> str:
    """Load a vehicle_dictionary_legacy2 bronze snapshot into the silver layer.

    Args:
        snapshot_date (datetime.date | None): Bronze snapshot date to
            load. Defaults to the most recent snapshot found under
            `bronze_root`.

    Returns:
        str: The fully-qualified silver table loaded into.

    """
    return _load_snapshot(
        source="vehicle_dictionary_legacy2",
        table=_LEGACY2_TABLE,
        columns=_LEGACY2_COLUMNS,
        parent_ddl=_LEGACY2_PARENT_DDL,
        indexes=_LEGACY2_INDEXES,
        snapshot_date=snapshot_date,
    )


def load_device_dictionary(snapshot_date: datetime.date | None = None) -> str:
    """Load a device_dictionary bronze snapshot into the silver layer.

    Args:
        snapshot_date (datetime.date | None): Bronze snapshot date to
            load. Defaults to the most recent snapshot found under
            `bronze_root`.

    Returns:
        str: The fully-qualified silver table loaded into.

    """
    return _load_snapshot(
        source="device_dictionary",
        table=_DEVICE_DICTIONARY_TABLE,
        columns=_DEVICE_DICTIONARY_COLUMNS,
        parent_ddl=_DEVICE_DICTIONARY_PARENT_DDL,
        indexes=_DEVICE_DICTIONARY_INDEXES,
        snapshot_date=snapshot_date,
    )


def load_2018(snapshot_date: datetime.date | None = None) -> str:
    """Load a vehicle_dictionary_2018 bronze snapshot into the silver layer.

    Args:
        snapshot_date (datetime.date | None): Bronze snapshot date to
            load. Defaults to the most recent snapshot found under
            `bronze_root`.

    Returns:
        str: The fully-qualified silver table loaded into.

    """
    return _load_snapshot(
        source="vehicle_dictionary_2018",
        table=_2018_TABLE,
        columns=_VEHICLE_DICTIONARY_COLUMNS,
        parent_ddl=_2018_PARENT_DDL,
        indexes=_VEHICLE_DICTIONARY_INDEXES,
        snapshot_date=snapshot_date,
    )


def load_antigo(snapshot_date: datetime.date | None = None) -> str:
    """Load a vehicle_dictionary_antigo bronze snapshot into the silver layer.

    Args:
        snapshot_date (datetime.date | None): Bronze snapshot date to
            load. Defaults to the most recent snapshot found under
            `bronze_root`.

    Returns:
        str: The fully-qualified silver table loaded into.

    """
    return _load_snapshot(
        source="vehicle_dictionary_antigo",
        table=_ANTIGO_TABLE,
        columns=_VEHICLE_DICTIONARY_COLUMNS,
        parent_ddl=_ANTIGO_PARENT_DDL,
        indexes=_VEHICLE_DICTIONARY_INDEXES,
        snapshot_date=snapshot_date,
    )
