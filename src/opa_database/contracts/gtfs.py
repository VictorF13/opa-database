"""Pandera schemas for the raw GTFS bronze source.

One `DataFrameModel` per GTFS table, following the standard GTFS reference
columns, plus one adapter-added field: `CalendarDatesSchema` and
`StopTimesSchema` each carry a `copied_from_feed_version_date` column
recording when that table's data was substituted from a different export
(see those classes' docstrings and `docs/architecture.md`).
`stops_unicode.txt` is deliberately excluded: it is a UTF-16 duplicate of
`stops.txt` kept for legacy consumers, not a distinct table.

All ID-like fields (route_id, stop_id, trip_id, shape_id, service_id,
fare_id, block_id) are kept as strings since some carry meaningful leading
zeros (e.g. route_id "0004"). `arrival_time`/`departure_time` are kept as
raw strings too, since GTFS allows values past "24:00:00" for trips
crossing midnight, which don't round-trip through a time type.
"""

import pandera.polars as pa
import polars as pl


class AgencySchema(pa.DataFrameModel):
    """`agency.txt`: transit agencies operating the feed."""

    agency_id: str = pa.Field(nullable=True)
    agency_name: str
    agency_url: str
    agency_timezone: str
    agency_lang: str = pa.Field(nullable=True)
    agency_phone: str = pa.Field(nullable=True)
    agency_fare_url: str = pa.Field(nullable=True)

    class Config:
        """Coerce raw CSV columns into their target dtypes."""

        coerce = True


class CalendarSchema(pa.DataFrameModel):
    """`calendar.txt`: weekly service patterns."""

    service_id: str
    monday: int
    tuesday: int
    wednesday: int
    thursday: int
    friday: int
    saturday: int
    sunday: int
    start_date: pl.Date
    end_date: pl.Date

    class Config:
        """Coerce raw CSV columns into their target dtypes."""

        coerce = True


class CalendarDatesSchema(pa.DataFrameModel):
    """`calendar_dates.txt`: exceptions to the base weekly service patterns.

    A handful of raw exports are missing this file entirely; for those,
    `adapters/gtfs.py` substitutes the nearest other export's data and
    stamps `copied_from_feed_version_date` with that export's date (see
    `docs/architecture.md`). Null for every normally-sourced row.
    """

    service_id: str
    date: pl.Date
    exception_type: int
    copied_from_feed_version_date: pl.Date = pa.Field(nullable=True)

    class Config:
        """Coerce raw CSV columns into their target dtypes."""

        coerce = True


class FareAttributesSchema(pa.DataFrameModel):
    """`fare_attributes.txt`: fare prices and transfer rules."""

    fare_id: str
    price: float
    currency_type: str
    payment_method: int
    transfers: int = pa.Field(nullable=True)
    transfer_duration: int = pa.Field(nullable=True)

    class Config:
        """Coerce raw CSV columns into their target dtypes."""

        coerce = True


class FareRulesSchema(pa.DataFrameModel):
    """`fare_rules.txt`: maps fares to routes/zones."""

    fare_id: str
    route_id: str = pa.Field(nullable=True)
    origin_id: str = pa.Field(nullable=True)
    destination_id: str = pa.Field(nullable=True)
    contains_id: str = pa.Field(nullable=True)

    class Config:
        """Coerce raw CSV columns into their target dtypes."""

        coerce = True


class RoutesSchema(pa.DataFrameModel):
    """`routes.txt`: transit routes."""

    route_id: str
    agency_id: str = pa.Field(nullable=True)
    route_short_name: str = pa.Field(nullable=True)
    route_long_name: str = pa.Field(nullable=True)
    route_desc: str = pa.Field(nullable=True)
    route_type: int
    route_url: str = pa.Field(nullable=True)
    route_color: str = pa.Field(nullable=True)
    route_text_color: str = pa.Field(nullable=True)

    class Config:
        """Coerce raw CSV columns into their target dtypes."""

        coerce = True


class ShapesSchema(pa.DataFrameModel):
    """`shapes.txt`: route shape polylines."""

    shape_id: str
    shape_pt_lat: float
    shape_pt_lon: float
    shape_pt_sequence: int
    shape_dist_traveled: float = pa.Field(nullable=True)

    class Config:
        """Coerce raw CSV columns into their target dtypes."""

        coerce = True


class StopsSchema(pa.DataFrameModel):
    """`stops.txt`: stop/station locations."""

    stop_id: str
    stop_code: str = pa.Field(nullable=True)
    stop_name: str
    stop_desc: str = pa.Field(nullable=True)
    stop_lat: float
    stop_lon: float
    zone_id: str = pa.Field(nullable=True)
    stop_url: str = pa.Field(nullable=True)
    location_type: int = pa.Field(nullable=True)
    parent_station: str = pa.Field(nullable=True)
    stop_timezone: str = pa.Field(nullable=True)
    wheelchair_boarding: int = pa.Field(nullable=True)

    class Config:
        """Coerce raw CSV columns into their target dtypes."""

        coerce = True


class StopTimesSchema(pa.DataFrameModel):
    """`stop_times.txt`: per-trip, per-stop arrival/departure times.

    One raw export is missing this file entirely; for that one,
    `adapters/gtfs.py` substitutes the nearest other export's data and
    stamps `copied_from_feed_version_date` with that export's date (see
    `docs/architecture.md`). Null for every normally-sourced row.
    """

    trip_id: str
    arrival_time: str = pa.Field(nullable=True)
    departure_time: str = pa.Field(nullable=True)
    stop_id: str
    stop_sequence: int
    stop_headsign: str = pa.Field(nullable=True)
    pickup_type: int = pa.Field(nullable=True)
    drop_off_type: int = pa.Field(nullable=True)
    shape_dist_traveled: float = pa.Field(nullable=True)
    copied_from_feed_version_date: pl.Date = pa.Field(nullable=True)

    class Config:
        """Coerce raw CSV columns into their target dtypes."""

        coerce = True


class TripsSchema(pa.DataFrameModel):
    """`trips.txt`: individual vehicle trips along a route."""

    route_id: str
    service_id: str
    trip_id: str
    trip_headsign: str = pa.Field(nullable=True)
    trip_short_name: str = pa.Field(nullable=True)
    direction_id: int = pa.Field(nullable=True)
    block_id: str = pa.Field(nullable=True)
    shape_id: str = pa.Field(nullable=True)
    wheelchair_accessible: int = pa.Field(nullable=True)

    class Config:
        """Coerce raw CSV columns into their target dtypes."""

        coerce = True


TABLES: dict[str, type[pa.DataFrameModel]] = {
    "agency": AgencySchema,
    "calendar": CalendarSchema,
    "calendar_dates": CalendarDatesSchema,
    "fare_attributes": FareAttributesSchema,
    "fare_rules": FareRulesSchema,
    "routes": RoutesSchema,
    "shapes": ShapesSchema,
    "stops": StopsSchema,
    "stop_times": StopTimesSchema,
    "trips": TripsSchema,
}
