"""Process-level settings read from the environment (HLD §9).

``GEMINI_API_KEY`` comes from the environment or a git-ignored ``.env`` file. It is held as a
``SecretStr`` so it never appears in logs, reprs, or persisted artifacts.
"""

from pathlib import Path
from typing import Final

from pydantic import Field, SecretStr

from iterplay.models.base import StrictModel

DEFAULT_MODEL: Final = "gemini-4-argon"
DEFAULT_RUNS_DIR: Final = Path("runs")
DEFAULT_GAME_PORT: Final = 8737
DEFAULT_VIEWER_PORT: Final = 8738
DEFAULT_ENV_FILE: Final = Path(".env")

API_KEY_ENV: Final = "GEMINI_API_KEY"
ENV_PREFIX: Final = "ITERPLAY_"
"""Prefix of the optional overrides: ``ITERPLAY_MODEL``, ``ITERPLAY_RUNS_DIR``,
``ITERPLAY_GAME_PORT`` and ``ITERPLAY_VIEWER_PORT``."""


class Settings(StrictModel):
    """Settings shared by every command.

    Servers always bind to 127.0.0.1; only their ports are configurable.
    """

    gemini_api_key: SecretStr | None = None
    model: str = DEFAULT_MODEL
    runs_dir: Path = DEFAULT_RUNS_DIR
    game_port: int = Field(default=DEFAULT_GAME_PORT, ge=1, le=65535)
    viewer_port: int = Field(default=DEFAULT_VIEWER_PORT, ge=1, le=65535)


def load_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> Settings:
    """Build settings from the environment, also reading ``env_file`` when it exists.

    Real environment variables take precedence over ``env_file``. A missing API key is not an
    error here because only ``iterplay new`` needs one (see :func:`require_api_key`).

    Raises:
        ConfigError: A variable is set to an invalid value.
    """
    raise NotImplementedError


def require_api_key(settings: Settings) -> SecretStr:
    """Return the Gemini API key.

    Raises:
        ConfigError: ``GEMINI_API_KEY`` is not set.
    """
    raise NotImplementedError
