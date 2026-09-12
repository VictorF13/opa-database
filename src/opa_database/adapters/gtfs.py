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
    from collections.abc import Iterable
    from pathlib import Path

# GTFS export filenames use several distinct historical naming schemes.
# Each entry pairs a pattern with the (year, month, day) index into that
# pattern's match groups, since schemes differ in both separator and
# day/month/year ordering.
_EXPORT_NAME_PATTERNS: tuple[tuple[re.Pattern[str], tuple[int, int, int]], ...] = (
    # 2020+: exportacao_YYYY-MM-DD.zip
    (re.compile(r"exportacao_(\d{4})-(\d{2})-(\d{2})\.zip$"), (0, 1, 2)),
    # 2015-2018 (most legacy exports): exportacaoDDMMYYYY.zip, sometimes
    # with a stray space before the date (e.g. "exportacao 25052018.zip")
    (re.compile(r"exportacao ?(\d{2})(\d{2})(\d{4})\.zip$"), (2, 1, 0)),
    # 2019 (roughly a third of that year's exports): exportacao_DD-MM-YYYY.zip
    (re.compile(r"exportacao_(\d{2})-(\d{2})-(\d{4})\.zip$"), (2, 1, 0)),
)

# One raw filename has a data-entry typo in the year ("2818" instead of
# "2018" -- the file lives in the 2018/ folder and its zip content is
# otherwise unremarkable), corrected explicitly rather than trusted as-is.
_FILENAME_YEAR_CORRECTIONS: dict[str, int] = {"exportacao 05072818.zip": 2018}


def _match_export_name(name: str) -> tuple[int, int, int] | None:
    """Parse (year, month, day) from a GTFS export filename.

    Tries every entry in `_EXPORT_NAME_PATTERNS` in turn, since export
    filenames aren't consistent across the raw archive's history.

    Args:
        name (str): Filename to parse (e.g. "exportacao_2022-01-15.zip").

    Returns:
        tuple[int, int, int] | None: `(year, month, day)` if `name`
            matches a known pattern, else `None`.

    """
    for pattern, (year_idx, month_idx, day_idx) in _EXPORT_NAME_PATTERNS:
        match = pattern.match(name)
        if match is None:
            continue
        groups = match.groups()
        year = _FILENAME_YEAR_CORRECTIONS.get(name, int(groups[year_idx]))
        return year, int(groups[month_idx]), int(groups[day_idx])
    return None


# Tables where a missing raw file is tolerated: `ingest` substitutes the
# nearest other export's data instead of failing (see `_read_table_for_export`
# and `docs/architecture.md`). Every other table still fails loudly on a
# missing file, since that's an untested, unvetted code path for them.
_SUBSTITUTABLE_TABLES = frozenset({"calendar_dates", "stop_times"})


def _all_export_zips() -> list[Path]:
    """List every GTFS export zip across all years, any known naming scheme.

    Unlike `_find_export_zips` (scoped to one year/month), this scans
    every year directory, so it can be used to search for a substitute
    export when a table is missing from a given export's own zip.
    """
    root = settings.raw_data_root / "GTFS"
    return sorted(
        path
        for year_dir in root.iterdir()
        if year_dir.is_dir()
        for path in year_dir.glob("exportacao*.zip")
        if _match_export_name(path.name) is not None
    )


def _find_export_zips(year: int, month: int) -> list[Path]:
    """Locate GTFS export zips for a given year/month.

    Matches any of `_EXPORT_NAME_PATTERNS` (the 2020+ convention plus the
    2015-2019 legacy naming schemes).
    """
    return [
        path
        for path in _all_export_zips()
        if (parsed := _match_export_name(path.name))
        and parsed[0] == year
        and parsed[1] == month
    ]


def _export_date(path: Path) -> datetime.date:
    parsed = _match_export_name(path.name)
    if parsed is None:
        msg = f"Unrecognized GTFS export filename: {path.name}"
        raise ValueError(msg)
    year, month, day = parsed
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


def _select_nearest[T](
    target_date: datetime.date,
    candidates: Iterable[tuple[datetime.date, T]],
) -> T:
    """Pick the candidate date nearest `target_date`.

    Ties (two candidates equally far from `target_date`) prefer the
    earlier one. Generic over the second tuple element (a `Path` in real
    use) and kept free of I/O so it's directly unit-testable.

    Args:
        target_date (datetime.date): Date to measure distance from.
        candidates (Iterable[tuple[datetime.date, T]]): `(date, value)`
            pairs to choose among.

    Returns:
        T: The value of the nearest candidate.

    """

    def sort_key(item: tuple[datetime.date, T]) -> tuple[int, int]:
        candidate_date, _ = item
        distance = abs((candidate_date - target_date).days)
        tie_break = 0 if candidate_date <= target_date else 1
        return (distance, tie_break)

    return min(candidates, key=sort_key)[1]


def _find_substitute_export(target: Path, table: str) -> Path:
    """Find the nearest other export whose zip actually contains `table`.

    Args:
        target (Path): The export zip missing `table`.
        table (str): Bronze table name (e.g. "calendar_dates").

    Returns:
        Path: The nearest other export zip (see `_select_nearest`) whose
            archive contains `f"{table}.txt"`.

    Raises:
        FileNotFoundError: If no other export contains `table`.

    """
    target_date = _export_date(target)
    candidates: list[tuple[datetime.date, Path]] = []
    for path in _all_export_zips():
        if path == target:
            continue
        with zipfile.ZipFile(path) as archive:
            if f"{table}.txt" in archive.namelist():
                candidates.append((_export_date(path), path))
    if not candidates:
        msg = f"No GTFS export contains {table}.txt to substitute for {target.name}."
        raise FileNotFoundError(msg)
    return _select_nearest(target_date, candidates)


def _read_table_for_export(
    archive: zipfile.ZipFile, zip_path: Path, table: str
) -> pl.DataFrame:
    """Read one table for one export, substituting for a missing raw file.

    Only `_SUBSTITUTABLE_TABLES` get this treatment; every other table
    still raises on a missing file, since that's an untested, unvetted
    code path for them. When a substitution happens, every row of the
    result is stamped with `copied_from_feed_version_date` set to the
    substitute export's date; a normally-sourced table gets that column
    stamped `null` instead, so the column always exists with consistent
    semantics whether or not this export needed a substitution.

    Args:
        archive (zipfile.ZipFile): This export's own open archive.
        zip_path (Path): This export's own zip path (used to locate a
            substitute and to know which export we're reading for).
        table (str): Bronze table name.

    Returns:
        pl.DataFrame: The table's data, with `copied_from_feed_version_date`
            added for tables in `_SUBSTITUTABLE_TABLES`.

    """
    if table not in _SUBSTITUTABLE_TABLES:
        return _read_table(archive, table)

    if f"{table}.txt" in archive.namelist():
        return _read_table(archive, table).with_columns(
            pl.lit(None, dtype=pl.Date).alias("copied_from_feed_version_date")
        )

    substitute_path = _find_substitute_export(zip_path, table)
    with zipfile.ZipFile(substitute_path) as substitute_archive:
        df = _read_table(substitute_archive, table)
    return df.with_columns(
        pl.lit(_export_date(substitute_path)).alias("copied_from_feed_version_date")
    )


def ingest(year: int, month: int) -> list[Path]:
    """Ingest all GTFS export snapshots for a year/month into the bronze layer.

    Each export zip is a full feed snapshot rather than daily data, so
    every table is partitioned by the export date rather than a calendar
    day of service.

    A few raw exports are missing an entire table file (e.g. no
    `calendar_dates.txt`). For tables in `_SUBSTITUTABLE_TABLES`, this is
    tolerated: the nearest other export's data is substituted, and every
    row it writes is tagged via `copied_from_feed_version_date` (see
    `_read_table_for_export` and `docs/architecture.md`). Any other table
    missing its file still raises.

    Args:
        year (int): Calendar year to ingest.
        month (int): Calendar month to ingest.

    Returns:
        list[Path]: Paths of the bronze parquet files written.

    """
    written: list[Path] = []
    for zip_path in _find_export_zips(year, month):
        date = _export_date(zip_path)
        partitions = {"year": date.year, "month": date.month, "day": date.day}
        with zipfile.ZipFile(zip_path) as archive:
            for table, schema in TABLES.items():
                df = schema.validate(_read_table_for_export(archive, zip_path, table))
                path = write_bronze(df, source=f"gtfs/{table}", partitions=partitions)
                written.append(path)
    return written
