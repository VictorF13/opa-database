"""Generic writer for partitioned bronze-layer parquet files."""

from __future__ import annotations

from typing import TYPE_CHECKING

from opa_database.config import settings

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import polars as pl


def write_bronze(
    df: pl.DataFrame,
    source: str,
    partitions: Mapping[str, str | int],
) -> Path:
    """Write a DataFrame as a single bronze parquet file, Hive-partitioned on disk.

    Args:
        df: Data to write, already validated against its source contract.
        source: Top-level directory name under `bronze_root` (e.g. "avl").
        partitions: Ordered partition key/value pairs (e.g. {"year": 2023,
            "month": 11, "day": 1}), rendered as `key=value` path segments.

    Returns:
        The path the parquet file was written to.

    """
    segments = [f"{key}={value}" for key, value in partitions.items()]
    directory = settings.bronze_root.joinpath(source, *segments)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "data.parquet"
    df.write_parquet(path)
    return path
