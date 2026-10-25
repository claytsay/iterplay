"""Streaming JSONL I/O for ``<bot>.telemetry.jsonl`` (HLD §4 [3])."""

from pathlib import Path
from types import TracebackType
from typing import Self

from iterplay.models.telemetry import TelemetryRecord


class TelemetryWriter:
    """Appends one :class:`TelemetryRecord` per line.

    Each line is flushed as it is written, so a crashed session loses at most one snapshot.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> Self:
        """Open the file for writing, creating parent directories."""
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError

    def append(self, record: TelemetryRecord) -> None:
        raise NotImplementedError


def read_telemetry(path: Path) -> list[TelemetryRecord]:
    """Load every record from a telemetry file, in order."""
    raise NotImplementedError
