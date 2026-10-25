"""Artifacts are written atomically as UTF-8, and models round-trip through JSON."""

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from iterplay.contract.state import GameState
from iterplay.models.run import RunIndex
from iterplay.models.smoke import SmokeCheck, SmokeCheckResult, SmokeReport
from iterplay.models.telemetry import TelemetryRecord
from iterplay.storage.artifacts import read_model, write_model, write_text


@pytest.fixture
def report() -> SmokeReport:
    return SmokeReport(
        passed=False,
        checks=[SmokeCheckResult(check=SmokeCheck.READY, passed=False, detail="timed out ★")],
        uncaught_errors=["TypeError: player is undefined"],
        attempt=2,
    )


def test_write_model_writes_indented_json_and_a_trailing_newline(
    tmp_path: Path, report: SmokeReport
) -> None:
    path = tmp_path / "smoke.json"

    write_model(path, report)

    assert path.read_bytes() == (report.model_dump_json(indent=2) + "\n").encode("utf-8")


def test_write_model_creates_parent_directories(tmp_path: Path, report: SmokeReport) -> None:
    path = tmp_path / "runs" / "abc" / "v1" / "smoke.json"

    write_model(path, report)

    assert read_model(path, SmokeReport) == report


def test_write_model_uses_each_models_field_aliases(
    tmp_path: Path, hld_state: dict[str, Any]
) -> None:
    record = TelemetryRecord(video_t=0.5, state=GameState.model_validate(hld_state))
    path = tmp_path / "record.json"

    write_model(path, record)

    text = path.read_text(encoding="utf-8")
    assert '"levelComplete": false' in text
    assert "level_complete" not in text
    assert read_model(path, TelemetryRecord) == record


def test_write_model_replaces_an_existing_artifact(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    write_model(path, RunIndex(run_ids=["a", "b"]))

    write_model(path, RunIndex(run_ids=["c"]))

    assert read_model(path, RunIndex) == RunIndex(run_ids=["c"])


def test_write_text_writes_exact_utf8(tmp_path: Path) -> None:
    text = "Patch notes ★ für v2\r\n- fixed the wall\n"
    path = tmp_path / "v2" / "changelog.md"

    write_text(path, text)

    assert path.read_bytes() == text.encode("utf-8")


def test_write_text_writes_empty_files(tmp_path: Path) -> None:
    path = tmp_path / "changelog.md"

    write_text(path, "")

    assert path.read_bytes() == b""


def test_writes_leave_no_temporary_files(tmp_path: Path, report: SmokeReport) -> None:
    write_model(tmp_path / "smoke.json", report)
    write_model(tmp_path / "smoke.json", report)
    write_text(tmp_path / "changelog.md", "- fixed")

    assert sorted(path.name for path in tmp_path.iterdir()) == ["changelog.md", "smoke.json"]


def test_a_write_renames_a_temporary_file_from_the_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "changelog.md"
    write_text(path, "old")
    renames: list[tuple[Path, Path]] = []
    real_replace = Path.replace

    def spy(source: Path, target: str | os.PathLike[str]) -> Path:
        renames.append((source, Path(target)))
        assert source.read_text(encoding="utf-8") == "new"
        assert path.read_text(encoding="utf-8") == "old"
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", spy)

    write_text(path, "new")

    [(source, target)] = renames
    assert target == path
    assert source.parent == path.parent
    assert source.name.startswith(".")
    assert path.read_text(encoding="utf-8") == "new"


def test_a_failed_write_keeps_the_old_artifact_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "changelog.md"
    write_text(path, "old")

    def fail(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", fail)

    with pytest.raises(OSError, match="disk full"):
        write_text(path, "new")

    assert path.read_text(encoding="utf-8") == "old"
    assert [entry.name for entry in tmp_path.iterdir()] == ["changelog.md"]


def test_a_write_never_deletes_a_temporary_file_it_did_not_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    squatters: list[Path] = []
    real_open = Path.open

    def squatting_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if "x" in mode:
            self.write_bytes(b"another writer's data")
            squatters.append(self)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", squatting_open)

    with pytest.raises(FileExistsError):
        write_text(tmp_path / "changelog.md", "new")

    [squatter] = squatters
    assert squatter.read_bytes() == b"another writer's data"
    assert not (tmp_path / "changelog.md").exists()


def test_read_model_raises_file_not_found_for_a_missing_artifact(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_model(tmp_path / "missing.json", RunIndex)


@pytest.mark.parametrize(
    "content",
    [b"", b"not json", b'{"run_ids": "abc"}', b'{"run_ids": [], "extra": 1}', b"\xff\xfe"],
)
def test_read_model_raises_validation_error_for_invalid_content(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "index.json"
    path.write_bytes(content)

    with pytest.raises(ValidationError):
        read_model(path, RunIndex)
