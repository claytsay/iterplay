"""Streaming JSONL I/O for ``<bot>.telemetry.jsonl`` (HLD §4 [3])."""

import json
from pathlib import Path
from types import TracebackType
from typing import IO, Self

from pydantic import ValidationError
from pydantic_core import ErrorDetails, to_jsonable_python

from iterplay.models.telemetry import TelemetryRecord


class TelemetryWriter:
    """Appends one :class:`TelemetryRecord` per line.

    Each line is flushed as it is written, so a crashed session loses at most one snapshot.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: IO[str] | None = None

    def __enter__(self) -> Self:
        """Open the file for writing, creating parent directories.

        Any earlier contents are replaced: each session writes a fresh file.

        Raises:
            RuntimeError: The writer is already open.
        """
        if self._file is not None:
            raise RuntimeError(f"The telemetry writer for {self._path} is already open")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("w", encoding="utf-8", newline="\n")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the file."""
        file, self._file = self._file, None
        if file is not None:
            file.close()

    def append(self, record: TelemetryRecord) -> None:
        """Write ``record`` as one compact JSON line and flush it to the operating system.

        Raises:
            RuntimeError: The writer is not open; use it as a context manager.
        """
        if self._file is None:
            raise RuntimeError(
                f"The telemetry writer for {self._path} is not open; append inside its with block"
            )
        self._file.write(_encode(record) + "\n")
        self._file.flush()


def read_telemetry(path: Path) -> list[TelemetryRecord]:
    """Load every record from a telemetry file, in order.

    Blank lines are skipped. A last line that is not even valid JSON is a snapshot cut short by
    a crash mid-write, so it is ignored.

    Raises:
        FileNotFoundError: ``path`` does not exist.
        ValueError: Any other line is not a valid record; the message names its line number.
    """
    lines = [
        (number, line)
        for number, line in enumerate(path.read_bytes().splitlines(), start=1)
        if line.strip()
    ]
    records: list[TelemetryRecord] = []
    for index, (number, line) in enumerate(lines):
        try:
            records.append(TelemetryRecord.model_validate_json(line))
        except ValidationError as exc:
            if index == len(lines) - 1 and _is_unparsable(exc):
                break
            raise ValueError(
                f"{path} line {number} is not a valid telemetry record: "
                f"{_describe(exc.errors()[0])}"
            ) from exc
    return records


def _encode(record: TelemetryRecord) -> str:
    """Serialise ``record`` as compact JSON that :func:`read_telemetry` can always load.

    Non-finite floats stay ``NaN`` or ``Infinity``, which pydantic parses back, instead of
    pydantic's own ``null``, which would not validate: a buggy game may well report a NaN
    position or an infinite frame rate. Non-ASCII text is escaped, so every line is ASCII.
    """
    return json.dumps(
        record.model_dump(),
        allow_nan=True,
        separators=(",", ":"),
        default=to_jsonable_python,
    )


def _is_unparsable(error: ValidationError) -> bool:
    return any(detail["type"] == "json_invalid" for detail in error.errors())


def _describe(detail: ErrorDetails) -> str:
    location = ".".join(str(part) for part in detail["loc"])
    return f"{location}: {detail['msg']}" if location else detail["msg"]
