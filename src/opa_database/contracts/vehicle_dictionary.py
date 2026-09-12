"""Pandera schemas for the vehicle dictionary family of bronze sources.

`DICIONÁRIO_VEÍCULOS/` on the raw data root held several independently
extracted vehicle-identity crosswalks, all now fully captured in bronze
(see `adapters/vehicle_dictionary.py` for why the raw files themselves
are gone). All four schemas below live in this one module, same as how
`contracts/avl.py` is one schema shared across AVL's several raw layouts
-- except here the raw *shapes* differ too, so each gets its own model
rather than a shared one:

- `VehicleDictionarySchema` (`cod_veiculo`/`id_veiculo`): the original
  mapping (`veiculos_atuais.csv`), plus two older snapshots that happen
  to share this exact shape (`veiculos2018.csv`, `veiculos_antigo.csv`).
  `id_veiculo` matches AVL's `vehicle_id` int.
- `VehicleDictionaryLegacySchema` (`vehicleid`/`numbus`,
  `dicionario_veiculos.csv`) and `VehicleDictionaryLegacy2Schema`
  (`id`/`carro`/`obs1`/`obs2`, `dicionario_veiculos2.csv`): two older,
  never-ingested exports with their own shapes and their own,
  much-smaller id ranges -- presumably an earlier GPS hardware
  generation. Nothing confirms either id space lines up with modern
  `vehicle_id`/`device_id` values, so reconciling them is deferred to a
  later layer, same as this module's own `cod_veiculo` ambiguity below.
- `DeviceDictionarySchema` (`codigo`/`ID`/.../`Número de Ordem`/...): a
  distinct bridge from the other three. It maps the GPS/AVL-side
  *device* id (matching `avl_pings.device_id`, e.g. "ep1-428113843") to
  the vehicle's real fleet number (matching AFC's `vehicle_number`),
  rather than AVL's opaque internal `vehicle_id` int. `device_id` rides
  along on every AVL ping but isn't used for any matching today.

`cod_veiculo` is not a unique key: buses get reassigned, so ~2% of codes
map to more than one `id_veiculo` over time. Bronze keeps that as-is;
reconciling which mapping is current is left for a later layer.
"""

import pandera.polars as pa


class VehicleDictionarySchema(pa.DataFrameModel):
    """Loose validation for a raw AFC-code-to-GPS-id vehicle mapping row."""

    cod_veiculo: str
    id_veiculo: str

    class Config:
        """Coerce raw string columns into their target dtypes."""

        coerce = True


class VehicleDictionaryLegacySchema(pa.DataFrameModel):
    """Loose validation for a raw legacy vehicleid-to-numbus mapping row."""

    vehicleid: str
    numbus: str

    class Config:
        """Coerce raw string columns into their target dtypes."""

        coerce = True


class VehicleDictionaryLegacy2Schema(pa.DataFrameModel):
    """Loose validation for a raw legacy id-to-carro mapping row, with notes."""

    id: str
    carro: str
    # Nullable: empty on all but one observed row ("DESATIVADO").
    obs1: str = pa.Field(nullable=True)
    # Nullable: empty on most rows; populated rows look like dates
    # (e.g. "15/10/2014"), kept as text since no format is confirmed.
    obs2: str = pa.Field(nullable=True)

    class Config:
        """Coerce raw string columns into their target dtypes."""

        coerce = True


class DeviceDictionarySchema(pa.DataFrameModel):
    """Loose validation for a raw device-id-to-vehicle-number mapping row.

    `codigo` and `device_id` are each unique within a snapshot when
    present (verified against the first extraction); `codigo` and
    `vehicle_number` are usually but not always identical (2 divergent
    rows out of 2219 in that same extraction, e.g. a stray leading zero
    or trailing letter on one side).
    """

    codigo: str
    # Nullable: support/utility vehicles (trailers, spare motos) often have
    # no GPS device fitted at all.
    device_id: str = pa.Field(nullable=True)
    vehicle_type: str
    company: str
    # Nullable: plate is missing for a large share of rows in the source.
    plate: str = pa.Field(nullable=True)
    vehicle_number: str
    status: str
    # Nullable: present in the raw header but empty on every observed row.
    action: str = pa.Field(nullable=True)

    class Config:
        """Coerce raw string columns into their target dtypes."""

        coerce = True
