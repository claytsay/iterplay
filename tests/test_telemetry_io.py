"""Telemetry streams one flushed JSON line per snapshot and survives a crash mid-write."""

import json
import math
from pathlib import Path
from typing import Any

import pytest

from iterplay.contract.state import GameState
from iterplay.models.telemetry import TelemetryRecord
from iterplay.storage.telemetry import TelemetryWriter, read_telemetry


def make_record(state: dict[str, Any], video_t: float, **changes: Any) -> TelemetryRecord:
    return TelemetryRecord(video_t=video_t, state=GameState.model_validate({**state, **changes}))


@pytest.fixture
def records(hld_state: dict[str, Any]) -> list[TelemetryRecord]:
    return [make_record(hld_state, step / 10, t=step / 10, deaths=step) for step in range(3)]


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "playtest" / "speedrunner.telemetry.jsonl"


def write_all(path: Path, records: list[TelemetryRecord]) -> None:
    with TelemetryWriter(path) as writer:
        for record in records:
            writer.append(record)


def test_records_round_trip_in_order(path: Path, records: list[TelemetryRecord]) -> None:
    write_all(path, records)

    assert read_telemetry(path) == records


def test_each_record_is_one_compact_json_line(path: Path, records: list[TelemetryRecord]) -> None:
    write_all(path, records)

    *lines, last = path.read_text(encoding="utf-8").split("\n")

    assert last == ""
    assert len(lines) == len(records)
    for line, record in zip(lines, records, strict=True):
        assert json.loads(line) == json.loads(record.model_dump_json())
        assert '"levelComplete":false' in line
        assert ": " not in line
        assert ", " not in line


def test_each_line_is_flushed_as_it_is_written(path: Path, records: list[TelemetryRecord]) -> None:
    with TelemetryWriter(path) as writer:
        writer.append(records[0])
        assert read_telemetry(path) == records[:1]

        writer.append(records[1])
        assert read_telemetry(path) == records[:2]


def test_append_outside_the_with_block_raises(path: Path, records: list[TelemetryRecord]) -> None:
    writer = TelemetryWriter(path)

    with pytest.raises(RuntimeError, match="not open"):
        writer.append(records[0])

    with writer:
        writer.append(records[0])

    with pytest.raises(RuntimeError, match="not open"):
        writer.append(records[1])
    assert read_telemetry(path) == records[:1]


def test_entering_an_open_writer_raises(path: Path) -> None:
    with TelemetryWriter(path) as writer, pytest.raises(RuntimeError, match="already open"):
        writer.__enter__()


def test_the_file_is_closed_when_the_session_fails(
    path: Path, records: list[TelemetryRecord]
) -> None:
    writer = TelemetryWriter(path)

    with pytest.raises(KeyError), writer:
        writer.append(records[0])
        raise KeyError("browser crashed")

    with pytest.raises(RuntimeError):
        writer.append(records[1])
    assert read_telemetry(path) == records[:1]


def test_a_new_session_replaces_an_old_file(path: Path, records: list[TelemetryRecord]) -> None:
    path.parent.mkdir(parents=True)
    path.write_text("stale contents\n", encoding="utf-8")

    write_all(path, records[:1])

    assert read_telemetry(path) == records[:1]


def test_read_skips_blank_lines(path: Path, records: list[TelemetryRecord]) -> None:
    first, second = (record.model_dump_json() for record in records[:2])
    path.parent.mkdir(parents=True)
    path.write_bytes(f"\n{first}\r\n\n   \n{second}\n\n".encode())

    assert read_telemetry(path) == records[:2]


@pytest.mark.parametrize("cut", [1, 10, -1])
def test_a_last_line_cut_short_by_a_crash_is_ignored(
    path: Path, records: list[TelemetryRecord], cut: int
) -> None:
    write_all(path, records)
    partial = records[0].model_dump_json()[:cut]
    with path.open("a", encoding="utf-8") as file:
        file.write(partial)

    assert read_telemetry(path) == records


def test_a_cut_short_last_line_is_ignored_before_trailing_blank_lines(
    path: Path, records: list[TelemetryRecord]
) -> None:
    write_all(path, records[:1])
    with path.open("a", encoding="utf-8") as file:
        file.write('{"video_t": 0.2, "sta\n\n')

    assert read_telemetry(path) == records[:1]


def test_a_last_line_cut_inside_a_multibyte_character_is_ignored(
    path: Path, records: list[TelemetryRecord]
) -> None:
    write_all(path, records[:1])
    with path.open("ab") as file:
        file.write('{"video_t": 0.2, "note": "★'.encode()[:-1])

    assert read_telemetry(path) == records[:1]


def test_a_corrupt_line_before_the_end_raises_naming_its_line_number(
    path: Path, records: list[TelemetryRecord]
) -> None:
    first, second = (record.model_dump_json() for record in records[:2])
    path.parent.mkdir(parents=True)
    path.write_text(f"{first}\n\n{{not json\n{second}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 3"):
        read_telemetry(path)


def test_a_last_line_that_parses_but_is_not_a_record_raises(
    path: Path, records: list[TelemetryRecord]
) -> None:
    write_all(path, records[:2])
    with path.open("a", encoding="utf-8") as file:
        file.write('{"video_t": -1, "state": {}}\n')

    with pytest.raises(ValueError, match="line 3") as excinfo:
        read_telemetry(path)

    assert "video_t" in str(excinfo.value)


def test_an_empty_file_has_no_records(path: Path) -> None:
    with TelemetryWriter(path):
        pass

    assert path.read_bytes() == b""
    assert read_telemetry(path) == []


def test_reading_a_missing_file_raises_file_not_found(path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_telemetry(path)


def test_non_finite_numbers_are_rejected_on_read(path: Path, hld_state: dict[str, Any]) -> None:
    """The contract forbids non-finite numbers, so a record that bypassed validation fails."""
    record = make_record(hld_state, 1.0)
    broken = record.model_copy(update={"state": record.state.model_copy(update={"fps": math.inf})})

    write_all(path, [broken, record])

    with pytest.raises(ValueError, match="line 1"):
        read_telemetry(path)


def test_line_separators_inside_strings_do_not_split_records(
    path: Path, hld_state: dict[str, Any]
) -> None:
    note = "line\u2028separator, next\u0085line and a ★"
    record = make_record(hld_state, 1.0, events=[{"type": "death", "t": 1.0, "note": note}])

    write_all(path, [record])

    assert path.read_bytes().isascii()
    [loaded] = read_telemetry(path)
    assert loaded == record
    assert loaded.state.events[0].model_extra == {"note": note}
