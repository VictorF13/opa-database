"""Shared pytest setup.

Importing any adapter/loader module transitively imports
`opa_database.config`, which eagerly constructs the `settings` singleton
at module load time and requires `raw_data_root` to be set (it doesn't
need to exist on disk, just to be present as a field). Locally this is
satisfied by `.env` (gitignored), but CI has no `.env`, so test
collection itself would fail before a single test runs. Setting a
fallback here (the repo root) fixes that without touching CI config or
the production `.env` handling; `setdefault` leaves a real local
`RAW_DATA_ROOT`/`.env` untouched.
"""

import os
from pathlib import Path

os.environ.setdefault("RAW_DATA_ROOT", str(Path(__file__).parent.parent))
