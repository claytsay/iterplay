"""Replay mode (HLD §6): the demo's safety net."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from iterplay.models.playtest import BotName
from iterplay.models.run import VersionRecord
from iterplay.models.scoring import ScoreCard
from iterplay.models.verification import VerifiedCritique
from iterplay.storage.layout import RunPaths


@dataclass(frozen=True, slots=True)
class ReplayStep:
    record: VersionRecord
    score: ScoreCard

    verified: VerifiedCritique | None
    """None for versions that never booted."""

    changelog: str
    videos: Mapping[BotName, Path]


class Replayer:
    """Steps through a finished run using only artifacts on disk; never calls the API."""

    def __init__(self, run: RunPaths) -> None:
        self._run = run

    def steps(self) -> Iterator[ReplayStep]:
        """One step per version, in ``summary.json`` order.

        Raises:
            RunNotFoundError: The run has no ``summary.json``.
        """
        raise NotImplementedError
