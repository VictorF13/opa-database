"""Adapter for the AVL/GPS source: raw daily CSV to validated bronze parquet."""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

import polars as pl

from opa_database.config import settings
from opa_database.contracts.avl import RAW_COLUMNS, AvlSchema
from opa_database.loaders.bronze import write_bronze

if TYPE_CHECKING:
    import datetime
    from pathlib import Path

_PORTUGUESE_MONTHS = {
    1: "JANEIRO",
    2: "FEVEREIRO",
    3: "MARCO",
    4: "ABRIL",
    5: "MAIO",
    6: "JUNHO",
    7: "JULHO",
    8: "AGOSTO",
    9: "SETEMBRO",
    10: "OUTUBRO",
    11: "NOVEMBRO",
    12: "DEZEMBRO",
}


def _normalize(name: str) -> str:
    """Strip accents, digits and punctuation, keeping only uppercase letters."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return "".join(char for char in ascii_name.upper() if char.isalpha())


def _find_month_dir(year: int, month: int) -> Path:
    """Locate the raw AVL folder for a given year/month.

    Folder names are inconsistent across years and even within 2023 (e.g.
    "NOVEMBRO-2023", "ABRIL - 2023", "JULHO - 2023 -"), so this matches on
    the normalized Portuguese month name rather than the literal string.
    """
    year_dir = settings.raw_data_root / "DADOS_GPS" / str(year)
    target = _PORTUGUESE_MONTHS[month]
    for entry in year_dir.iterdir():
        if entry.is_dir() and _normalize(entry.name) == target:
            return entry
    msg = f"No AVL folder found for {year}-{month:02d} under {year_dir}"
    raise FileNotFoundError(msg)


def _read_raw_csv(path: Path) -> pl.DataFrame:
    df = pl.read_csv(path, has_header=False, new_columns=RAW_COLUMNS)
    timestamp = pl.col("metric_timestamp").cast(pl.Utf8)
    timestamp = timestamp.str.strptime(pl.Datetime, "%Y%m%d%H%M%S")
    return df.with_columns(timestamp)


def _write_day(df: pl.DataFrame, date: datetime.date) -> Path:
    partitions = {"year": date.year, "month": date.month, "day": date.day}
    return write_bronze(df, source="avl", partitions=partitions)


def ingest(year: int, month: int) -> list[Path]:
    """Ingest all raw AVL files for a year/month into the bronze layer.

    Each raw file is named after the day it was dumped, but pings logged
    just after midnight belong to the following day. To avoid splitting
    that spillover across two separate bronze writes for the same day
    (which would overwrite one another), the last date seen in each file is
    held back and merged with the next file before being written.

    Args:
        year (int): Calendar year to ingest.
        month (int): Calendar month to ingest.

    Returns:
        list[Path]: Paths of the bronze parquet files written.

    """
    month_dir = _find_month_dir(year, month)
    written: list[Path] = []
    carry: pl.DataFrame | None = None

    for csv_path in sorted(month_dir.glob("*.csv")):
        df = AvlSchema.validate(_read_raw_csv(csv_path))
        if carry is not None:
            df = pl.concat([carry, df])

        date_col = pl.col("metric_timestamp").dt.date()
        dates = df.select(date_col).to_series().unique().sort().to_list()
        for date in dates[:-1]:
            day_df = df.filter(pl.col("metric_timestamp").dt.date() == date)
            written.append(_write_day(day_df, date))
        carry = df.filter(pl.col("metric_timestamp").dt.date() == dates[-1])

    if carry is not None and not carry.is_empty():
        last_date = carry.select(pl.col("metric_timestamp").dt.date().first()).item()
        written.append(_write_day(carry, last_date))

    return written
