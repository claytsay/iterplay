"""Turns raw bot recordings into the clips the critic watches (HLD §4 [4])."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from iterplay.media.ffmpeg import FFmpeg
from iterplay.models.media import PreparedMedia
from iterplay.models.playtest import BotName
from iterplay.storage.layout import VersionPaths


@dataclass(frozen=True, slots=True)
class MediaPrepConfig:
    width: int = 640
    height: int = 360
    fps: int = 10
    title_card_s: float = 1.5


class MediaPreparer:
    """Labels, timestamps, downscales and joins bot recordings.

    Burns in the bot name and an mm:ss clock, so the critic's timestamps line up with
    telemetry, and downscales to 640x360 at 10 fps to keep video tokens low. The combined clip
    plays the runs in ``bots`` order, each introduced by a title card.
    """

    def __init__(self, ffmpeg: FFmpeg, config: MediaPrepConfig | None = None) -> None:
        self._ffmpeg = ffmpeg
        self._config = config or MediaPrepConfig()

    async def prepare(self, paths: VersionPaths, bots: Sequence[BotName]) -> PreparedMedia:
        """Write each bot's ``<bot>_360p.mp4`` and the ``combined_360p.mp4`` clip."""
        raise NotImplementedError

    async def side_by_side(
        self, left: Path, right: Path, output: Path, *, labels: tuple[str, str]
    ) -> None:
        """Compose two clips side by side, e.g. v1 against the latest version (HLD §11)."""
        raise NotImplementedError

    async def _annotate(self, source: Path, output: Path, bot: BotName) -> None:
        """Burn in the label and clock, then downscale and resample one recording."""
        raise NotImplementedError

    async def _title_card(self, bot: BotName, output: Path) -> None:
        raise NotImplementedError

    async def _concatenate(self, clips: Sequence[Path], output: Path) -> None:
        raise NotImplementedError
