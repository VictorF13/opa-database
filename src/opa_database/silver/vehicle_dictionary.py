"""Silver loader for the vehicle dictionary reference source."""

from __future__ import annotations

import datetime

import polars as pl

from opa_database.config import settings
from opa_database.loaders.silver import (
    IndexSpec,
    daily_partition_name,
    get_connection,
    replace_period,
)

_TABLE = "silver.vehicle_dictionary"

# Partitioned by day, matching this loader's own single-snapshot load
# calls (see loaders/silver.py::replace_period). Row counts here are tiny
# (a few thousand per snapshot), so partitioning is about consistency
# with the other silver tables rather than a real perf need.
_PARENT_DDL = """
CREATE TABLE IF NOT EXISTS silver.vehicle_dictionary (
    snapshot_date date NOT NULL,
    cod_veiculo text NOT NULL,
    id_veiculo text NOT NULL
) PARTITION BY RANGE (snapshot_date);
"""

# cod_veiculo is deliberately not unique, even within one snapshot: buses
# get reassigned (~2% of codes map to more than one id_veiculo). id_veiculo
# is unique within a snapshot, so it gets a real constraint as a
# data-integrity safeguard, same reasoning as AFC's event_id. Since each
# partition already holds exactly one snapshot, this per-partition index
# already covers the full "unique within a snapshot" guarantee — no
# weakening versus the old unpartitioned version, unlike AFC's event_id.
_INDEXES = (
    IndexSpec("snapshot_date_idx", unique=False, definition="(snapshot_date)"),
    IndexSpec("cod_veiculo_idx", unique=False, definition="(cod_veiculo)"),
    IndexSpec("snapshot_id_key", unique=True, definition="(snapshot_date, id_veiculo)"),
)

_COLUMNS = ("cod_veiculo", "id_veiculo")


def _find_latest_snapshot() -> datetime.date:
    root = settings.bronze_root / "vehicle_dictionary"
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
        msg = f"No vehicle_dictionary bronze snapshots found under {root}"
        raise FileNotFoundError(msg)
    return max(dates)


def load(snapshot_date: datetime.date | None = None) -> None:
    """Load a vehicle dictionary bronze snapshot into the silver layer.

    Defaults to the most recent bronze snapshot available. Keyed by its
    own ingestion date (`snapshot_date`) rather than a calendar period —
    same "period = bronze's own partition key" pattern as AFC's dump_date
    and GTFS's feed_version_date.

    Args:
        snapshot_date (datetime.date | None): Bronze snapshot date to
            load. Defaults to the most recent snapshot found under
            `bronze_root`.

    """
    date = snapshot_date or _find_latest_snapshot()
    path = (
        settings.bronze_root
        / "vehicle_dictionary"
        / f"year={date.year}"
        / f"month={date.month}"
        / f"day={date.day}"
        / "data.parquet"
    )
    df = (
        pl.scan_parquet(path)
        .with_columns(pl.lit(date).alias("snapshot_date"))
        .select("snapshot_date", *_COLUMNS)
        .collect()
    )

    partition = daily_partition_name("vehicle_dictionary", date)
    with get_connection() as conn:
        replace_period(
            conn,
            _TABLE,
            partition,
            df,
            partition_start=date,
            partition_end=date + datetime.timedelta(days=1),
            parent_ddl=_PARENT_DDL,
            indexes=_INDEXES,
        )
