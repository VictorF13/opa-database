"""Adapter for the vehicle dictionary source: a reference CSV snapshot.

Unlike AVL/GTFS/AFC, this isn't a time series of raw files to pick a
year/month from — it's a single, currently-live reference file on disk
that gets updated in place over time. So instead of partitioning by a date
parsed out of the raw data, each ingestion run snapshots the file as of
today (or an explicitly given date), preserving the mapping as it existed
at that point without overwriting previous snapshots.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import polars as pl

from opa_database.config import settings
from opa_database.contracts.vehicle_dictionary import VehicleDictionarySchema
from opa_database.loaders.bronze import write_bronze

if TYPE_CHECKING:
    from pathlib import Path

_RAW_RELATIVE_PATH = "DICIONÁRIO_VEÍCULOS/veiculos_atuais.csv"


def ingest(snapshot_date: datetime.date | None = None) -> Path:
    """Snapshot the current vehicle dictionary into the bronze layer.

    Returns:
        The path of the bronze parquet file written.

    """
    date = snapshot_date or datetime.datetime.now(tz=datetime.UTC).date()
    path = settings.raw_data_root / _RAW_RELATIVE_PATH
    df = pl.read_csv(path, separator=";", infer_schema_length=0)
    validated = VehicleDictionarySchema.validate(df)
    partitions = {"year": date.year, "month": date.month, "day": date.day}
    return write_bronze(validated, source="vehicle_dictionary", partitions=partitions)
