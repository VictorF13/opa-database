"""Pandera schema for the raw AFC/bilhetagem bronze source.

The raw feed is a deeply nested XML per day:
`Movimentos > MovimentoDiario > Categoria > Empresa > Veiculo > Linha >
Viagem > Passageiro`, where each `Passageiro` is one boarding/fare event.
This schema describes the flattened, one-row-per-`Passageiro` shape the
adapter produces, carrying every ancestor's attributes down as columns.

Each daily raw file is a delayed-upload backlog, not just that day's data:
validators without live connectivity buffer transactions locally and
upload them whenever they reconnect, so a file can contain
`MovimentoDiario` entries (identified by `service_date`) going back weeks
or months. This isn't a resend/correction system — `event_id` is globally
unique across dumps, so each transaction uploads exactly once, just
possibly late. Bronze keeps this as-is — `service_date` is a plain column,
not a partition key. See `adapters/afc.py` for the reasoning on why
partitioning is by the dump file's date instead.

ID-like fields (company_code, vehicle_number, validator_id, line_number,
line_operator_number, event_id, card_id, stop codes) are kept as strings:
some observed values carry leading zeros (e.g. company_code "035"), and
sibling reference data (`DICIONARIO_VEICULOS`) shows vehicle codes that
aren't purely numeric at all.
"""

import pandera.polars as pa
import polars as pl


class AfcSchema(pa.DataFrameModel):
    """Loose validation for a flattened AFC/bilhetagem boarding event."""

    service_date: pl.Date

    company_code: str
    company_modality: int

    category_type: int

    vehicle_number: str
    # Nullable: a couple of dumps (e.g. 2021-01-10/11) have this attribute
    # blank for most rows, a real gap in the raw feed rather than a
    # parsing bug -- the rest of the row is still usable.
    validator_id: str = pa.Field(nullable=True)

    line_number: str
    line_shift: int
    line_operator_number: str
    line_fare_table: int
    line_opened_at: pl.Datetime
    line_closed_at: pl.Datetime

    trip_opened_at: pl.Datetime
    trip_closed_at: pl.Datetime
    turnstile_start: int
    turnstile_end: int
    direction: int
    stop_open: str
    stop_close: str

    boarding_at: pl.Datetime
    integration_bum: int
    integration_type: int
    event_id: str
    sigben: int
    passenger_type: int
    card_id: str
    fare_paid: float
    subsidy_value: float
    metro_transfer_value: float
    latitude: float = pa.Field(nullable=True)
    longitude: float = pa.Field(nullable=True)

    class Config:
        """Coerce raw string columns into their target dtypes."""

        coerce = True
