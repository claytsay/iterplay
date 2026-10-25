"""Process-level settings read from the environment (HLD §9).

``GEMINI_API_KEY`` comes from the environment or a git-ignored ``.env`` file. It is held as a
``SecretStr`` so it never appears in logs, reprs, or persisted artifacts.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

from dotenv import dotenv_values
from pydantic import Field, SecretStr, ValidationError

from iterplay.errors import ConfigError
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

_VARIABLES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "gemini_api_key": API_KEY_ENV,
        "model": f"{ENV_PREFIX}MODEL",
        "runs_dir": f"{ENV_PREFIX}RUNS_DIR",
        "game_port": f"{ENV_PREFIX}GAME_PORT",
        "viewer_port": f"{ENV_PREFIX}VIEWER_PORT",
    }
)
"""The environment variable behind each :class:`Settings` field."""


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

    Real environment variables take precedence over ``env_file``, which is read without
    changing ``os.environ``. Blank values count as unset, and values are stripped of
    surrounding whitespace. A missing API key is not an error here because only
    ``iterplay new`` needs one (see :func:`require_api_key`).

    Raises:
        ConfigError: ``env_file`` cannot be read, or a variable is set to an invalid value.
    """
    file_values = _read_env_file(env_file)
    values = {
        field: value
        for field, variable in _VARIABLES.items()
        if (value := _lookup(variable, file_values)) is not None
    }
    try:
        return Settings.model_validate(values)
    except ValidationError as exc:
        raise ConfigError(_describe(exc)) from None


def require_api_key(settings: Settings) -> SecretStr:
    """Return the Gemini API key.

    Raises:
        ConfigError: ``GEMINI_API_KEY`` is not set.
    """
    key = settings.gemini_api_key
    if key is None or not key.get_secret_value().strip():
        raise ConfigError(
            f"{API_KEY_ENV} is not set. Export it, or add it to a git-ignored .env file "
            "(see .env.example)."
        )
    return key


def _read_env_file(env_file: Path | None) -> Mapping[str, str | None]:
    if env_file is None:
        return {}
    try:
        return dotenv_values(env_file) if env_file.exists() else {}
    except OSError as exc:
        raise ConfigError(f"Cannot read {env_file}: {exc.strerror or exc}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"Cannot read {env_file}: it is not UTF-8 text") from None


def _lookup(variable: str, file_values: Mapping[str, str | None]) -> str | None:
    """The stripped value of ``variable`` from the environment, else from the env file."""
    for value in (os.environ.get(variable), file_values.get(variable)):
        if value is not None and value.strip():
            return value.strip()
    return None


def _describe(error: ValidationError) -> str:
    """Name the variable behind each invalid field, never echoing the API key."""
    problems: list[str] = []
    for detail in error.errors():
        field = str(detail["loc"][0]) if detail["loc"] else ""
        variable = _VARIABLES.get(field, field)
        got = "" if variable == API_KEY_ENV else f" (got {detail['input']!r})"
        problems.append(f"{variable} is invalid: {detail['msg']}{got}")
    return "; ".join(problems)
