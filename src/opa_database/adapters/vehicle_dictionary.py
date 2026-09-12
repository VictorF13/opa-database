"""Adapters for the vehicle dictionary family of bronze sources.

`DICIONÁRIO_VEÍCULOS/` on the raw data root held several independently
extracted vehicle-identity crosswalks. All of them are ingested from this
one module, same as how `adapters/avl.py` is one script handling AVL's
several raw layouts across years -- except here, unlike AVL, the raw
files don't all funnel into a single shared target schema, so each
`ingest_*` function below pairs with its own model in
`contracts/vehicle_dictionary.py`. See that module's docstring for what
each source actually maps and why they're kept separate rather than
merged.

None of these are a time series of raw files to pick a year/month from --
each is a single, already-final CSV, snapshotted as of whenever it gets
ingested (or an explicitly given date) rather than a date parsed out of
the raw data itself, preserving the mapping as it existed at that point
without overwriting previous snapshots. `veiculos_atuais.csv` (the source
behind `ingest_vehicle`) used to be the one exception -- a file that got
updated in place over time -- but all five raw files were fully captured
into bronze and then deleted from raw_data_root (kept only as an external
backup) once nothing was left un-ingested, so every source here is now
equally a one-time, static capture with no live file left behind it to
re-run against.
"""

from __future__ import annotations

import datetime
import io
from typing import TYPE_CHECKING

import polars as pl

from opa_database.config import settings
from opa_database.contracts.vehicle_dictionary import (
    DeviceDictionarySchema,
    VehicleDictionaryLegacy2Schema,
    VehicleDictionaryLegacySchema,
    VehicleDictionarySchema,
)
from opa_database.loaders.bronze import write_bronze

if TYPE_CHECKING:
    from pathlib import Path

_VEHICLE_RAW_RELATIVE_PATH = "DICIONÁRIO_VEÍCULOS/veiculos_atuais.csv"
_LEGACY_RAW_RELATIVE_PATH = "DICIONÁRIO_VEÍCULOS/dicionario_veiculos.csv"
_LEGACY2_RAW_RELATIVE_PATH = "DICIONÁRIO_VEÍCULOS/dicionario_veiculos2.csv"
_2018_RAW_RELATIVE_PATH = "DICIONÁRIO_VEÍCULOS/veiculos2018.csv"
_ANTIGO_RAW_RELATIVE_PATH = "DICIONÁRIO_VEÍCULOS/veiculos_antigo.csv"

# Raw header, in order: Código, ID, Tipo de Veículo, Empresa, Placa,
# Número de Ordem, Situação, Ação.
_DEVICE_COLUMN_RENAME = {
    "Código": "codigo",
    "ID": "device_id",
    "Tipo de Veículo": "vehicle_type",
    "Empresa": "company",
    "Placa": "plate",
    "Número de Ordem": "vehicle_number",
    "Situação": "status",
    "Ação": "action",
}


def _today() -> datetime.date:
    return datetime.datetime.now(tz=datetime.UTC).date()


def ingest_vehicle(snapshot_date: datetime.date | None = None) -> Path:
    """Snapshot the vehicle dictionary into the bronze layer.

    Source: `veiculos_atuais.csv` (`cod_veiculo`/`id_veiculo`).

    Args:
        snapshot_date (datetime.date | None): Date to record the snapshot
            as. Defaults to today (UTC) if not given.

    Returns:
        Path: The path of the bronze parquet file written.

    """
    date = snapshot_date or _today()
    path = settings.raw_data_root / _VEHICLE_RAW_RELATIVE_PATH
    df = pl.read_csv(path, separator=";", infer_schema_length=0)
    validated = VehicleDictionarySchema.validate(df)
    partitions = {"year": date.year, "month": date.month, "day": date.day}
    return write_bronze(validated, source="vehicle_dictionary", partitions=partitions)


def ingest_legacy(snapshot_date: datetime.date | None = None) -> Path:
    """Snapshot the first legacy vehicle dictionary into the bronze layer.

    Source: `dicionario_veiculos.csv` (`vehicleid`/`numbus`).

    Args:
        snapshot_date (datetime.date | None): Date to record the snapshot
            as. Defaults to today (UTC) if not given.

    Returns:
        Path: The path of the bronze parquet file written.

    """
    date = snapshot_date or _today()
    path = settings.raw_data_root / _LEGACY_RAW_RELATIVE_PATH
    df = pl.read_csv(path, separator=";", infer_schema_length=0)
    validated = VehicleDictionaryLegacySchema.validate(df)
    partitions = {"year": date.year, "month": date.month, "day": date.day}
    return write_bronze(
        validated, source="vehicle_dictionary_legacy", partitions=partitions
    )


def ingest_legacy2(snapshot_date: datetime.date | None = None) -> Path:
    """Snapshot the second legacy vehicle dictionary into the bronze layer.

    Source: `dicionario_veiculos2.csv` (`id`/`carro`/`obs1`/`obs2`).

    Args:
        snapshot_date (datetime.date | None): Date to record the snapshot
            as. Defaults to today (UTC) if not given.

    Returns:
        Path: The path of the bronze parquet file written.

    """
    date = snapshot_date or _today()
    path = settings.raw_data_root / _LEGACY2_RAW_RELATIVE_PATH
    df = pl.read_csv(path, separator=";", infer_schema_length=0)
    validated = VehicleDictionaryLegacy2Schema.validate(df)
    partitions = {"year": date.year, "month": date.month, "day": date.day}
    return write_bronze(
        validated, source="vehicle_dictionary_legacy2", partitions=partitions
    )


def ingest_2018(snapshot_date: datetime.date | None = None) -> Path:
    """Snapshot the 2018 vehicle dictionary into the bronze layer.

    Source: `veiculos2018.csv` (`id_veiculo`/`cod_veiculo`, same shape as
    `veiculos_atuais.csv`, reusing `VehicleDictionarySchema`). Kept as
    its own bronze source rather than merged into `vehicle_dictionary`:
    nothing confirms this file's `id_veiculo` values share an id space
    with `veiculos_atuais.csv`'s.

    Args:
        snapshot_date (datetime.date | None): Date to record the snapshot
            as. Defaults to today (UTC) if not given.

    Returns:
        Path: The path of the bronze parquet file written.

    """
    date = snapshot_date or _today()
    path = settings.raw_data_root / _2018_RAW_RELATIVE_PATH
    df = pl.read_csv(path, separator=";", infer_schema_length=0)
    validated = VehicleDictionarySchema.validate(df)
    partitions = {"year": date.year, "month": date.month, "day": date.day}
    return write_bronze(
        validated, source="vehicle_dictionary_2018", partitions=partitions
    )


def ingest_antigo(snapshot_date: datetime.date | None = None) -> Path:
    """Snapshot the "antigo" vehicle dictionary into the bronze layer.

    Source: `veiculos_antigo.csv` (`cod_veiculo`/`id_veiculo`, same shape
    as `veiculos_atuais.csv`, reusing `VehicleDictionarySchema`). Its
    `id_veiculo` values (single/double digits) are far smaller than
    either `veiculos_atuais.csv`'s or AVL's, so it is presumably an even
    earlier snapshot than `veiculos2018.csv` -- kept as its own bronze
    source for the same reason. Unlike the other raw dictionary files,
    this one is ISO-8859-1 encoded, not UTF-8, so it's read as bytes and
    transcoded before handing off to Polars.

    Args:
        snapshot_date (datetime.date | None): Date to record the snapshot
            as. Defaults to today (UTC) if not given.

    Returns:
        Path: The path of the bronze parquet file written.

    """
    date = snapshot_date or _today()
    path = settings.raw_data_root / _ANTIGO_RAW_RELATIVE_PATH
    text = path.read_bytes().decode("iso-8859-1")
    df = pl.read_csv(io.StringIO(text), separator=";", infer_schema_length=0)
    validated = VehicleDictionarySchema.validate(df)
    partitions = {"year": date.year, "month": date.month, "day": date.day}
    return write_bronze(
        validated, source="vehicle_dictionary_antigo", partitions=partitions
    )


def ingest_device_dictionary(snapshot_date: datetime.date | None = None) -> Path:
    """Snapshot a dated device dictionary export into the bronze layer.

    Source: `device_dictionary_{date}.csv`. Unlike the other sources in
    this module, this one has no single current path -- each extraction
    is its own dated CSV export that never gets updated after the fact,
    so `snapshot_date` is required rather than defaulting to today (kept
    as an `| None` type, same as every other function here, so the CLI
    can hold one uniform source-name -> ingest-function mapping; the
    "actually required" part is enforced at runtime instead).

    Args:
        snapshot_date (datetime.date | None): Date the export was
            extracted, matching the `{date}` in
            `device_dictionary_{date}.csv`. Required -- there is no
            "current" file to default to.

    Returns:
        Path: The path of the bronze parquet file written.

    Raises:
        ValueError: If `snapshot_date` is not given.
        FileNotFoundError: If no export exists for `snapshot_date`.

    """
    if snapshot_date is None:
        msg = (
            "device_dictionary has no single current file -- pass the "
            "snapshot_date of the specific dated export to ingest."
        )
        raise ValueError(msg)
    path = (
        settings.raw_data_root
        / "DICIONÁRIO_VEÍCULOS"
        / f"device_dictionary_{snapshot_date.isoformat()}.csv"
    )
    if not path.exists():
        msg = f"No device dictionary export found for {snapshot_date} at {path}"
        raise FileNotFoundError(msg)
    df = pl.read_csv(path, infer_schema_length=0).rename(_DEVICE_COLUMN_RENAME)
    validated = DeviceDictionarySchema.validate(df)
    partitions = {
        "year": snapshot_date.year,
        "month": snapshot_date.month,
        "day": snapshot_date.day,
    }
    return write_bronze(validated, source="device_dictionary", partitions=partitions)
