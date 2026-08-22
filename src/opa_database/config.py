"""Module for defining initial settings and the singleton."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings for the pipeline.

    Settings are configured to be automatically read from environment variables, then
    a `.env` file, if they are not passed here.

    `raw_data_root` is not checked for existence here: only the
    bronze-ingest code path actually reads from disk, so a missing/wrong
    path surfaces as a `FileNotFoundError` there instead, rather than
    blocking every other use of `settings` (e.g. `db_dsn`-only scripts)
    up front.

    Attributes:
        raw_data_root (Path): Path where the full raw data directory is located at.
        bronze_root (Path, optional): Path where the bronze layer parquets will be
            written to. Defaults to a local `./data/bronze`.
        db_dsn (str, optional): Connection string for the PostgreSQL+PostGIS
            database, provisioned via `docker compose up -d`. Shared by every
            schema (`silver`, `ml`, ...), not just `silver`.

    """

    raw_data_root: Path
    bronze_root: Path = Path("./data/bronze")
    db_dsn: str = "postgresql://opa:opa@localhost:5432/opa"

    # extra="ignore": `.env` also carries DB_USER/PASSWORD/NAME, read
    # directly by `docker compose` for variable substitution rather than by
    # this class (which only needs the composed `db_dsn`).
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


# I'm ignoring the missing argument here because it's auto-populated
settings = Settings()  # ty:ignore[missing-argument]
