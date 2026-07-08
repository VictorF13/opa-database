"""Pandera schema for the raw AVL/GPS bronze source.

Column order and meaning come from `DADOS_GPS/GPS data fields.txt` on
`raw_data_root`: direction, latitude, longitude, metrictimestamp (UTC-0),
odometer, routecode, speed, device_deviceid, vehicle_vehicleid. The raw
files ship with no header row.
"""

import pandera.polars as pa
import polars as pl  # noqa: TC002 -- needed at runtime, pandera reads real dtypes off the annotations

RAW_COLUMNS = [
    "direction",
    "latitude",
    "longitude",
    "metric_timestamp",
    "odometer",
    "route_code",
    "speed",
    "device_id",
    "vehicle_id",
]


class AvlSchema(pa.DataFrameModel):
    """Loose validation for a single day of raw AVL/GPS pings."""

    direction: int
    latitude: float = pa.Field(nullable=True)
    longitude: float = pa.Field(nullable=True)
    metric_timestamp: pl.Datetime
    odometer: int
    route_code: int
    speed: int
    device_id: str
    vehicle_id: int

    class Config:
        """Coerce raw string/int columns read from CSV into their target dtypes."""

        coerce = True
