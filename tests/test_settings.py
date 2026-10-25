"""Settings come from the environment and an optional .env file, and never leak the key (HLD §9)."""

import os
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from iterplay.errors import ConfigError
from iterplay.settings import (
    API_KEY_ENV,
    DEFAULT_GAME_PORT,
    DEFAULT_MODEL,
    DEFAULT_RUNS_DIR,
    DEFAULT_VIEWER_PORT,
    Settings,
    load_settings,
    require_api_key,
)

FAKE_KEY = "fake-test-key-0123456789"
MODEL_ENV = "ITERPLAY_MODEL"
RUNS_DIR_ENV = "ITERPLAY_RUNS_DIR"
GAME_PORT_ENV = "ITERPLAY_GAME_PORT"
VIEWER_PORT_ENV = "ITERPLAY_VIEWER_PORT"
VARIABLES = (API_KEY_ENV, MODEL_ENV, RUNS_DIR_ENV, GAME_PORT_ENV, VIEWER_PORT_ENV)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Hide real settings, and run in an empty directory so the default ``.env`` is ours."""
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    return tmp_path / "test.env"


def write_env(path: Path, values: dict[str, str]) -> Path:
    path.write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8"
    )
    return path


def test_defaults_apply_when_nothing_is_set(env_file: Path) -> None:
    settings = load_settings(env_file)

    assert settings == Settings()
    assert settings.gemini_api_key is None
    assert (settings.model, settings.runs_dir, settings.game_port, settings.viewer_port) == (
        DEFAULT_MODEL,
        DEFAULT_RUNS_DIR,
        DEFAULT_GAME_PORT,
        DEFAULT_VIEWER_PORT,
    )


def test_reads_every_variable_from_the_env_file(env_file: Path) -> None:
    write_env(
        env_file,
        {
            API_KEY_ENV: FAKE_KEY,
            MODEL_ENV: "gemini-test",
            RUNS_DIR_ENV: "out/runs",
            GAME_PORT_ENV: "9001",
            VIEWER_PORT_ENV: "9002",
        },
    )

    settings = load_settings(env_file)

    assert require_api_key(settings).get_secret_value() == FAKE_KEY
    assert (settings.model, settings.runs_dir, settings.game_port, settings.viewer_port) == (
        "gemini-test",
        Path("out/runs"),
        9001,
        9002,
    )


def test_reads_every_variable_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, FAKE_KEY)
    monkeypatch.setenv(MODEL_ENV, "gemini-env")
    monkeypatch.setenv(RUNS_DIR_ENV, "/tmp/iterplay-runs")
    monkeypatch.setenv(GAME_PORT_ENV, "18737")
    monkeypatch.setenv(VIEWER_PORT_ENV, "18738")

    settings = load_settings(None)

    assert require_api_key(settings).get_secret_value() == FAKE_KEY
    assert (settings.model, settings.runs_dir, settings.game_port, settings.viewer_port) == (
        "gemini-env",
        Path("/tmp/iterplay-runs"),
        18737,
        18738,
    )


def test_environment_variables_win_over_the_env_file(
    monkeypatch: pytest.MonkeyPatch, env_file: Path
) -> None:
    write_env(env_file, {API_KEY_ENV: "file-key", MODEL_ENV: "from-file", GAME_PORT_ENV: "9001"})
    monkeypatch.setenv(API_KEY_ENV, FAKE_KEY)
    monkeypatch.setenv(MODEL_ENV, "from-env")

    settings = load_settings(env_file)

    assert require_api_key(settings).get_secret_value() == FAKE_KEY
    assert settings.model == "from-env"
    assert settings.game_port == 9001


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_environment_variable_falls_back_to_the_env_file(
    monkeypatch: pytest.MonkeyPatch, env_file: Path, blank: str
) -> None:
    write_env(env_file, {MODEL_ENV: "from-file"})
    monkeypatch.setenv(MODEL_ENV, blank)

    assert load_settings(env_file).model == "from-file"


def test_blank_values_in_the_env_file_count_as_unset(env_file: Path) -> None:
    write_env(env_file, {API_KEY_ENV: "", MODEL_ENV: "", GAME_PORT_ENV: ""})

    assert load_settings(env_file) == Settings()


def test_a_variable_without_a_value_counts_as_unset(env_file: Path) -> None:
    env_file.write_text(f"{API_KEY_ENV}\n{MODEL_ENV}=gemini-test\n", encoding="utf-8")

    settings = load_settings(env_file)

    assert settings.gemini_api_key is None
    assert settings.model == "gemini-test"


def test_values_are_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, f"  {FAKE_KEY}\n")
    monkeypatch.setenv(GAME_PORT_ENV, " 9001 ")

    settings = load_settings(None)

    assert require_api_key(settings).get_secret_value() == FAKE_KEY
    assert settings.game_port == 9001


def test_the_env_file_does_not_change_os_environ(env_file: Path) -> None:
    write_env(env_file, {API_KEY_ENV: FAKE_KEY, MODEL_ENV: "gemini-test"})

    load_settings(env_file)

    assert API_KEY_ENV not in os.environ
    assert MODEL_ENV not in os.environ


def test_a_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "absent.env") == Settings()


def test_the_default_env_file_is_dotenv_in_the_working_directory(tmp_path: Path) -> None:
    write_env(tmp_path / ".env", {MODEL_ENV: "from-dotenv"})

    assert load_settings().model == "from-dotenv"


def test_no_env_file_is_read_when_it_is_none(tmp_path: Path) -> None:
    write_env(tmp_path / ".env", {MODEL_ENV: "from-dotenv"})

    assert load_settings(None).model == DEFAULT_MODEL


@pytest.mark.parametrize("variable", [GAME_PORT_ENV, VIEWER_PORT_ENV])
@pytest.mark.parametrize("value", ["abc", "0", "-1", "65536", "80.5"])
def test_invalid_ports_raise_config_error_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ConfigError, match=variable) as excinfo:
        load_settings(None)

    assert repr(value) in str(excinfo.value)
    assert not isinstance(excinfo.value, ValidationError)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


def test_invalid_values_in_the_env_file_name_the_variable(env_file: Path) -> None:
    write_env(env_file, {VIEWER_PORT_ENV: "eighty"})

    with pytest.raises(ConfigError, match=VIEWER_PORT_ENV):
        load_settings(env_file)


def test_every_invalid_variable_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GAME_PORT_ENV, "0")
    monkeypatch.setenv(VIEWER_PORT_ENV, "99999")

    with pytest.raises(ConfigError) as excinfo:
        load_settings(None)

    assert GAME_PORT_ENV in str(excinfo.value)
    assert VIEWER_PORT_ENV in str(excinfo.value)


@pytest.mark.parametrize("port", ["1", "65535"])
def test_ports_at_the_bounds_are_valid(monkeypatch: pytest.MonkeyPatch, port: str) -> None:
    monkeypatch.setenv(GAME_PORT_ENV, port)

    assert load_settings(None).game_port == int(port)


def test_errors_never_show_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, FAKE_KEY)
    monkeypatch.setenv(GAME_PORT_ENV, "not-a-port")

    with pytest.raises(ConfigError) as excinfo:
        load_settings(None)

    assert FAKE_KEY not in str(excinfo.value)


def test_an_env_file_that_is_not_utf8_raises_config_error(env_file: Path) -> None:
    env_file.write_bytes(f"{API_KEY_ENV}={FAKE_KEY}\xff\n".encode("latin-1"))

    with pytest.raises(ConfigError, match="not UTF-8") as excinfo:
        load_settings(env_file)

    assert str(env_file) in str(excinfo.value)
    assert FAKE_KEY not in str(excinfo.value)


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs POSIX file permissions"
)
def test_an_unreadable_env_file_raises_config_error(env_file: Path) -> None:
    write_env(env_file, {MODEL_ENV: "gemini-test"})
    env_file.chmod(0)
    try:
        with pytest.raises(ConfigError, match="Cannot read"):
            load_settings(env_file)
    finally:
        env_file.chmod(0o600)


def test_require_api_key_returns_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, FAKE_KEY)

    key = require_api_key(load_settings(None))

    assert isinstance(key, SecretStr)
    assert key.get_secret_value() == FAKE_KEY


@pytest.mark.parametrize("settings", [Settings(), Settings(gemini_api_key=SecretStr("  "))])
def test_require_api_key_raises_config_error_naming_the_variable(settings: Settings) -> None:
    with pytest.raises(ConfigError, match=API_KEY_ENV):
        require_api_key(settings)


def test_the_key_never_appears_in_reprs_or_dumps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, FAKE_KEY)
    settings = load_settings(None)
    key = require_api_key(settings)

    shown = [repr(settings), str(settings), settings.model_dump_json(), repr(key), str(key)]

    assert all(FAKE_KEY not in text for text in shown)
