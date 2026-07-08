"""Pandera schema for the raw vehicle dictionary bronze source.

Maps the AFC-side vehicle code (`cod_veiculo`, e.g. "35251", or "DES 02002"
for decommissioned buses) to the GPS-side vehicle id (`id_veiculo`, matching
AVL's `vehicle_id`). `cod_veiculo` is not a unique key: buses get
reassigned, so ~2% of codes map to more than one `id_veiculo` over time.
Bronze keeps that as-is; reconciling which mapping is current is left for
a later layer.
"""

import pandera.polars as pa


class VehicleDictionarySchema(pa.DataFrameModel):
    """Loose validation for a raw AFC-code-to-GPS-id vehicle mapping row."""

    cod_veiculo: str
    id_veiculo: str

    class Config:
        """Coerce raw string columns into their target dtypes."""

        coerce = True
