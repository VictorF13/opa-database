"""Module for defining initial settings and the singleton."""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings for the pipeline.

    Settings are configured to be automatically read from environment variables, then
    a `.env` file, if they are not passed here.

    Attributes:
        raw_data_root (Path): Path where the full raw data directory is located at.
        bronze_root (Path, optional): Path where the bronze layer parquets will be
            written to. Defaults to a local `./data/bronze`.

    Raises:
        ValueError: If the `raw_data_root` passed or detected does not actually exist.

    """

    raw_data_root: Path
    bronze_root: Path = Path("./data/bronze")

    @field_validator("raw_data_root")
    @classmethod
    def _must_exist(cls, v: Path) -> Path:
        if not v.exists():
            msg = f"raw_data_root does not exist: {v}"
            raise ValueError(msg)
        return v

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


# I'm ignoring the missing argument here because it's auto-populated
settings = Settings()  # ty:ignore[missing-argument]
