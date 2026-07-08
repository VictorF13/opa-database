"""Adapter for the GTFS source: raw zipped feed exports to bronze parquet."""

from __future__ import annotations

import datetime
import io
import re
import zipfile
from typing import TYPE_CHECKING

import polars as pl

from opa_database.config import settings
from opa_database.contracts.gtfs import TABLES
from opa_database.loaders.bronze import write_bronze

if TYPE_CHECKING:
    from pathlib import Path

_EXPORT_NAME = re.compile(r"exportacao_(\d{4})-(\d{2})-(\d{2})\.zip$")


def _find_export_zips(year: int, month: int) -> list[Path]:
    """Locate GTFS export zips for a given year/month.

    Only matches the "exportacao_YYYY-MM-DD.zip" naming convention used
    since 2020. Earlier exports use several inconsistent naming schemes
    (e.g. "exportacao02102015.zip", "exportacao_02-08-2019.zip") and aren't
    supported yet.
    """
    year_dir = settings.raw_data_root / "GTFS" / str(year)
    matches = [
        path
        for path in year_dir.glob("exportacao_*.zip")
        if (match := _EXPORT_NAME.match(path.name)) and int(match.group(2)) == month
    ]
    return sorted(matches)


def _export_date(path: Path) -> datetime.date:
    match = _EXPORT_NAME.match(path.name)
    if match is None:
        msg = f"Unrecognized GTFS export filename: {path.name}"
        raise ValueError(msg)
    year, month, day = (int(part) for part in match.groups())
    return datetime.date(year, month, day)


# GTFS packs dates as a bare YYYYMMDD integer. Pandera's `coerce=True` would
# otherwise cast that integer straight to `pl.Date` as if it were an epoch
# day count, producing nonsense dates, so these columns are parsed explicitly.
_DATE_COLUMNS: dict[str, tuple[str, ...]] = {
    "calendar": ("start_date", "end_date"),
    "calendar_dates": ("date",),
}


def _parse_gtfs_date(column: str) -> pl.Expr:
    return pl.col(column).str.strptime(pl.Date, "%Y%m%d")


def _read_table(archive: zipfile.ZipFile, table: str) -> pl.DataFrame:
    # infer_schema_length=0 reads every column as a raw string: several IDs
    # (e.g. route_id "0004") carry meaningful leading zeros that polars'
    # normal type inference would silently strip by guessing them as ints.
    # Pandera's coerce=True then casts each column from that raw string into
    # its declared schema dtype.
    with archive.open(f"{table}.txt") as raw:
        df = pl.read_csv(io.BytesIO(raw.read()), infer_schema_length=0)
    date_columns = _DATE_COLUMNS.get(table, ())
    if date_columns:
        df = df.with_columns(*(_parse_gtfs_date(col) for col in date_columns))
    return df


def ingest(year: int, month: int) -> list[Path]:
    """Ingest all GTFS export snapshots for a year/month into the bronze layer.

    Each export zip is a full feed snapshot rather than daily data, so
    every table is partitioned by the export date rather than a calendar
    day of service.

    Returns:
        Paths of the bronze parquet files written.

    """
    written: list[Path] = []
    for zip_path in _find_export_zips(year, month):
        date = _export_date(zip_path)
        partitions = {"year": date.year, "month": date.month, "day": date.day}
        with zipfile.ZipFile(zip_path) as archive:
            for table, schema in TABLES.items():
                df = schema.validate(_read_table(archive, table))
                path = write_bronze(df, source=f"gtfs/{table}", partitions=partitions)
                written.append(path)
    return written
