"""Adapter for the AFC/bilhetagem source: nested XML zips to bronze parquet."""

from __future__ import annotations

import datetime
import re
import zipfile
from typing import TYPE_CHECKING
from xml.etree.ElementTree import iterparse

import polars as pl

from opa_database.config import settings
from opa_database.contracts.afc import AfcSchema
from opa_database.loaders.bronze import write_bronze

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import IO

_DUMP_FILE_NAME = re.compile(r"V(\d{4})(\d{2})(\d{2})t?\.zip$")

# One column per (context tag, XML attribute), flattened onto every
# Passageiro row. Order matches the nesting depth the value is read at.
_CONTEXT_ATTRS: tuple[tuple[str, str, str], ...] = (
    ("MovimentoDiario", "data_mov", "service_date"),
    ("Categoria", "Tipo", "category_type"),
    ("Empresa", "Codigo", "company_code"),
    ("Empresa", "modalidade", "company_modality"),
    ("Veiculo", "Numero", "vehicle_number"),
    ("Veiculo", "validador", "validator_id"),
    ("Linha", "Numero", "line_number"),
    ("Linha", "jornada", "line_shift"),
    ("Linha", "num_operador", "line_operator_number"),
    ("Linha", "tabela", "line_fare_table"),
    ("Linha", "hora_abertura", "line_opened_at"),
    ("Linha", "hora_fechamento", "line_closed_at"),
    ("Viagem", "data_hora_abertura", "trip_opened_at"),
    ("Viagem", "data_hora_fechamento", "trip_closed_at"),
    ("Viagem", "catraca_inicio", "turnstile_start"),
    ("Viagem", "catraca_final", "turnstile_end"),
    ("Viagem", "sentido", "direction"),
    ("Viagem", "ponto_abertura", "stop_open"),
    ("Viagem", "ponto_fechamento", "stop_close"),
)
_CONTEXT_TAGS = {tag for tag, _, _ in _CONTEXT_ATTRS}

_PASSENGER_ATTRS: tuple[tuple[str, str], ...] = (
    ("data_hora", "boarding_at"),
    ("integracao_bum", "integration_bum"),
    ("integracao", "integration_type"),
    ("evento", "event_id"),
    ("sigben", "sigben"),
    ("tipo", "passenger_type"),
    ("Matricula", "card_id"),
    ("valor_pago", "fare_paid"),
    ("valor_subsidio", "subsidy_value"),
    ("valor_repasse_metro", "metro_transfer_value"),
    ("latitude", "latitude"),
    ("longitude", "longitude"),
)

_COLUMNS = tuple(name for *_, name in _CONTEXT_ATTRS) + tuple(
    name for _, name in _PASSENGER_ATTRS
)


def _find_dump_zips(year: int, month: int) -> list[Path]:
    """Locate raw AFC daily dump zips for a given year/month.

    Only matches the "V{YYYYMMDD}.zip" naming convention used since 2020
    (a trailing "t" before ".zip" is also tolerated: 16 dumps in May 2022
    are named that way, e.g. "V20220513t.zip", the only month/year this
    has been observed). 2014-2018 raw data uses a different format
    entirely (per-month folders of "Viagenssigom{date}.csv" files) and
    isn't supported yet.
    """
    year_dir = settings.raw_data_root / "DADOS_BILHETAGEM" / str(year)
    matches = [
        path
        for path in year_dir.glob("V*.zip")
        if (match := _DUMP_FILE_NAME.match(path.name)) and int(match.group(2)) == month
    ]
    return sorted(matches)


def _dump_date(path: Path) -> datetime.date:
    match = _DUMP_FILE_NAME.match(path.name)
    if match is None:
        msg = f"Unrecognized AFC dump filename: {path.name}"
        raise ValueError(msg)
    year, month, day = (int(part) for part in match.groups())
    return datetime.date(year, month, day)


def _flatten(xml_source: IO[bytes]) -> Iterator[dict[str, str | None]]:
    """Flatten one AFC XML file into one dict per `Passageiro` boarding event.

    Ancestor attributes (service date, company, vehicle, line, trip) are
    captured when each container element opens and carried onto every
    `Passageiro` row emitted under it. Elements are cleared as soon as
    they're no longer needed to keep memory bounded on ~15M rows/month.
    """
    context: dict[str, str | None] = dict.fromkeys(_COLUMNS)

    # S314: the feed is our own transit agency's internal export, not
    # untrusted user input, so stdlib ElementTree (not defusedxml) is fine.
    for event, elem in iterparse(xml_source, events=("start", "end")):  # noqa: S314
        tag = elem.tag
        if event == "start":
            if tag in _CONTEXT_TAGS:
                for xml_tag, attr, column in _CONTEXT_ATTRS:
                    if xml_tag == tag:
                        context[column] = elem.attrib.get(attr) or None
        elif tag == "Passageiro":
            row = dict(context)
            for attr, column in _PASSENGER_ATTRS:
                row[column] = elem.attrib.get(attr) or None
            yield row
            elem.clear()
        elif tag != "Raiz":
            elem.clear()


def ingest(year: int, month: int) -> list[Path]:
    """Ingest all raw AFC dump files for a year/month into the bronze layer.

    Each dump file is a delayed-upload backlog covering many service
    dates, not a single day of data: validators without live connectivity
    buffer transactions locally and upload their backlog whenever they
    reconnect, so a dump named for the day it arrived can contain
    `service_date`s going back weeks. Every dump is written as its own
    bronze partition (keyed by the dump file's date) rather than split by
    `service_date`. This isn't a resend/correction system — `event_id` is
    globally unique across dumps (verified: zero overlap across all 435
    day-pairs in November 2023), so each transaction is uploaded exactly
    once, just possibly late.

    Args:
        year (int): Calendar year to ingest.
        month (int): Calendar month to ingest.

    Returns:
        list[Path]: Paths of the bronze parquet files written.

    """
    written: list[Path] = []
    for zip_path in _find_dump_zips(year, month):
        date = _dump_date(zip_path)
        partitions = {"year": date.year, "month": date.month, "day": date.day}
        with zipfile.ZipFile(zip_path) as archive:
            [xml_name] = archive.namelist()
            with archive.open(xml_name) as xml_file:
                rows = {column: [] for column in _COLUMNS}
                for row in _flatten(xml_file):
                    for column, value in row.items():
                        rows[column].append(value)
        df = pl.DataFrame(rows, schema=dict.fromkeys(_COLUMNS, pl.Utf8))
        validated = AfcSchema.validate(df)
        written.append(write_bronze(validated, source="afc", partitions=partitions))
    return written
