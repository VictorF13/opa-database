"""Silver loader for the vehicle dictionary reference source."""

from __future__ import annotations

import datetime

import polars as pl

from opa_database.config import settings
from opa_database.loaders.silver import get_connection, replace_period

_TABLE = "silver.vehicle_dictionary"

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS silver.vehicle_dictionary (
    snapshot_date date NOT NULL,
    cod_veiculo text NOT NULL,
    id_veiculo text NOT NULL
);
"""

# cod_veiculo is deliberately not unique, even within one snapshot: buses
# get reassigned (~2% of codes map to more than one id_veiculo). id_veiculo
# is unique within a snapshot, so it gets a real constraint as a
# data-integrity safeguard, same reasoning as AFC's event_id.
_INDEXES = (
    (
        "vehicle_dictionary_snapshot_date_idx",
        "CREATE INDEX vehicle_dictionary_snapshot_date_idx "
        "ON silver.vehicle_dictionary (snapshot_date);",
    ),
    (
        "vehicle_dictionary_cod_veiculo_idx",
        "CREATE INDEX vehicle_dictionary_cod_veiculo_idx "
        "ON silver.vehicle_dictionary (cod_veiculo);",
    ),
    (
        "vehicle_dictionary_snapshot_id_key",
        "CREATE UNIQUE INDEX vehicle_dictionary_snapshot_id_key "
        "ON silver.vehicle_dictionary (snapshot_date, id_veiculo);",
    ),
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

    with get_connection() as conn:
        replace_period(
            conn,
            _TABLE,
            df,
            time_column="snapshot_date",
            start=date,
            end=date + datetime.timedelta(days=1),
            table_ddl=_TABLE_DDL,
            indexes=_INDEXES,
        )
