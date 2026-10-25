"""Replay mode (HLD §6): the demo's safety net."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from iterplay.errors import RunNotFoundError
from iterplay.models.playtest import BotName
from iterplay.models.run import RunSummary, VersionRecord
from iterplay.models.scoring import ScoreCard
from iterplay.models.verification import VerifiedCritique
from iterplay.storage.artifacts import read_model
from iterplay.storage.layout import RunPaths, VersionPaths

_ModelT = TypeVar("_ModelT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ReplayStep:
    """One version of a finished run, as recorded on disk."""

    record: VersionRecord
    score: ScoreCard

    verified: VerifiedCritique | None
    """None for versions that never booted."""

    changelog: str
    """Patch notes from ``changelog.md``; empty for v1 or when the file is missing."""

    videos: Mapping[BotName, Path]
    """Raw recordings that exist, in :class:`BotName` order. Read-only."""


class Replayer:
    """Steps through a finished run using only artifacts on disk; never calls the API."""

    def __init__(self, run: RunPaths) -> None:
        self._run = run

    def steps(self) -> Iterator[ReplayStep]:
        """One step per version, in ``summary.json`` order.

        ``summary.json`` is read when this is called. Each version's artifacts are read only
        when iteration reaches it, so the versions before a damaged one still replay. Each
        call reads the run afresh.

        Raises:
            RunNotFoundError: The run has no ``summary.json``, or (during iteration) a version
                it lists has no ``score.json``.
            ValueError: ``summary.json``, or (during iteration) one of a version's artifacts,
                does not match its schema; the message names the file.
        """
        summary = _read(self._run.summary, RunSummary)
        if summary is None:
            raise RunNotFoundError(self._missing_summary())
        return (self._step(record) for record in summary.versions)

    def _step(self, record: VersionRecord) -> ReplayStep:
        version = self._run.version(record.version)
        score = _read(version.score, ScoreCard)
        if score is None:
            raise RunNotFoundError(
                f"Cannot replay v{record.version} of {self._run.run_id}: "
                f"{version.score} does not exist"
            )
        return ReplayStep(
            record=record,
            score=score,
            verified=_read(version.critique_verified, VerifiedCritique),
            changelog=_read_text(version.changelog),
            videos=_videos(version),
        )

    def _missing_summary(self) -> str:
        run = self._run
        if not run.root.is_dir():
            return f"Cannot replay {run.root}: the directory does not exist"
        if not run.config.is_file():
            return f"Cannot replay {run.root}: it is not a run, because it has no {run.config.name}"
        return f"Cannot replay {run.root}: it has no {run.summary.name}, so no version has finished"


def _read(path: Path, model_type: type[_ModelT]) -> _ModelT | None:
    """The artifact at ``path``, or None if there is no such file.

    Raises:
        ValueError: The file is not a valid ``model_type``; the message names the file.
    """
    try:
        return read_model(path, model_type)
    except FileNotFoundError:
        return None
    except ValidationError as exc:
        raise ValueError(f"{path} is not a valid {model_type.__name__}: {_describe(exc)}") from exc


def _read_text(path: Path) -> str:
    """A text artifact, or "" if there is no such file. Bytes that are not UTF-8 are replaced."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _videos(version: VersionPaths) -> Mapping[BotName, Path]:
    recordings = {bot: version.raw_video(bot) for bot in BotName}
    return MappingProxyType({bot: path for bot, path in recordings.items() if path.is_file()})


def _describe(error: ValidationError) -> str:
    """The first problem pydantic found, and how many others there are."""
    first = error.errors()[0]
    location = ".".join(str(part) for part in first["loc"])
    message = f"{location}: {first['msg']}" if location else first["msg"]
    others = error.error_count() - 1
    return f"{message} (and {others} more)" if others else message
