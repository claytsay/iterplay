"""Turns raw bot recordings into the clips the critic watches (HLD §4 [4])."""

import asyncio
import contextlib
import logging
import math
import tempfile
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from iterplay.errors import MediaError
from iterplay.media import text_images
from iterplay.media.ffmpeg import FFmpeg
from iterplay.models.media import PreparedMedia
from iterplay.models.playtest import BotName
from iterplay.storage.layout import VersionPaths

LABEL_OFFSET: Final = 10
"""Pixels from the top-left corner of the frame to the burned-in label."""

LABEL_SCALE: Final = 2
"""Video pixels per font pixel in burned-in labels."""

_CLOCK_FRAME: Final = "clock_%04d.png"
"""image2 pattern for the clock labels, one per second of a recording."""

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MediaPrepConfig:
    """Format of every prepared clip. H.264 with 4:2:0 chroma needs an even frame size."""

    width: int = 640
    height: int = 360
    fps: int = 10
    title_card_s: float = 1.5
    """How long each title card shows; 0 leaves the cards out."""

    def __post_init__(self) -> None:
        if min(self.width, self.height) < 2 or self.width % 2 or self.height % 2:
            raise ValueError(f"frame size must be even and positive: {self.width}x{self.height}")
        if self.fps < 1:
            raise ValueError(f"fps must be at least 1, not {self.fps}")
        if not math.isfinite(self.title_card_s) or self.title_card_s < 0:
            raise ValueError(f"title_card_s must be zero or more, not {self.title_card_s}")


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
        """Write each bot's ``<bot>_360p.mp4`` and the ``combined_360p.mp4`` clip.

        Recordings are prepared concurrently. A bot whose recording is missing or unusable is
        left out with a warning, so one bad run does not cost the critic the others.

        Raises:
            MediaError: No clip could be prepared, or joining the clips failed.
        """
        wanted = list(dict.fromkeys(bots))
        outcomes = await asyncio.gather(
            *(self._prepare_clip(paths, bot) for bot in wanted), return_exceptions=True
        )
        clips: dict[BotName, Path] = {}
        problems: list[str] = []
        for bot, outcome in zip(wanted, outcomes, strict=True):
            if isinstance(outcome, MediaError):
                _log.warning("leaving %s out of the prepared media: %s", bot, outcome)
                problems.append(f"{bot}: {outcome}")
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                clips[bot] = outcome
        if not clips:
            reasons = problems or ["no bots were given"]
            raise MediaError("\n".join(["no clip could be prepared:", *reasons]))
        await self._join(clips, paths.combined_video)
        return PreparedMedia(combined=paths.combined_video, per_bot=clips)

    async def side_by_side(
        self, left: Path, right: Path, output: Path, *, labels: tuple[str, str]
    ) -> None:
        """Compose two clips side by side, e.g. v1 against the latest version (HLD §11).

        Each side is fitted to the configured frame size and rate and labelled in its
        bottom-left corner, clear of the bot label a prepared clip already carries. The result
        is twice as wide and stops with the shorter clip.

        Raises:
            MediaError: ffmpeg failed, for example because an input is missing or unreadable.
        """
        with tempfile.TemporaryDirectory(prefix="iterplay-sbs-") as tmp:
            images = (Path(tmp) / "left.png", Path(tmp) / "right.png")
            await asyncio.to_thread(_write_labels, zip(images, labels, strict=True))
            at = f"x={LABEL_OFFSET}:y=H-h-{LABEL_OFFSET}"
            graph = (
                f"[0:v]{self._fit()}[left];[left][2:v]overlay={at}[a];"
                f"[1:v]{self._fit()}[right];[right][3:v]overlay={at}[b];"
                "[a][b]hstack=inputs=2:shortest=1[out]"
            )
            async with _replacing(output) as partial:
                await self._ffmpeg.run(
                    *("-i", left, "-i", right, "-i", images[0], "-i", images[1]),
                    *("-filter_complex", graph, "-map", "[out]"),
                    *self._encoding(),
                    partial,
                )

    async def _prepare_clip(self, paths: VersionPaths, bot: BotName) -> Path:
        source = paths.raw_video(bot)
        if not await asyncio.to_thread(source.is_file):
            raise MediaError(f"no recording at {source}")
        output = paths.prepared_video(bot)
        await self._annotate(source, output, bot)
        return output

    async def _annotate(self, source: Path, output: Path, bot: BotName) -> None:
        """Burn in the label and clock, then downscale and resample one recording.

        The clock is a sequence of labels, one per second of the recording, overlaid at 1 fps
        so it shows seconds since the recording started. Should the recording outlast its
        probed duration, the last label is held; either way the clip ends with the recording.
        """
        seconds = max(1, math.ceil(await self._ffmpeg.duration_s(source)))
        with tempfile.TemporaryDirectory(prefix="iterplay-clock-") as tmp:
            frames = Path(tmp)
            await asyncio.to_thread(_write_clock, frames, bot, seconds)
            graph = (
                f"[0:v]{self._fit()}[clip];"
                "[1:v]tpad=stop=-1:stop_mode=clone[clock];"
                f"[clip][clock]overlay={LABEL_OFFSET}:{LABEL_OFFSET}:shortest=1[out]"
            )
            async with _replacing(output) as partial:
                await self._ffmpeg.run(
                    *("-i", source),
                    *("-f", "image2", "-framerate", "1", "-i", _clock_pattern(frames)),
                    *("-filter_complex", graph, "-map", "[out]"),
                    *self._encoding(),
                    partial,
                )

    async def _title_card(self, bot: BotName, output: Path) -> None:
        """Encode a card naming ``bot``, ``title_card_s`` long, in the format of the clips."""
        config = self._config
        with tempfile.TemporaryDirectory(prefix="iterplay-card-") as tmp:
            image = Path(tmp) / "card.png"
            await asyncio.to_thread(_write_title_card, image, bot, config.width, config.height)
            await self._ffmpeg.run(
                *("-loop", "1", "-framerate", str(config.fps)),
                *("-t", f"{config.title_card_s:.3f}", "-i", image),
                *("-vf", "setsar=1"),
                *self._encoding(),
                output,
            )

    async def _concatenate(self, clips: Sequence[Path], output: Path) -> None:
        """Join clips of the same size and frame rate end to end with the concat filter."""
        inputs: list[str | Path] = []
        for clip in clips:
            inputs += ["-i", clip]
        streams = "".join(f"[{index}:v]" for index in range(len(clips)))
        async with _replacing(output) as partial:
            await self._ffmpeg.run(
                *inputs,
                *("-filter_complex", f"{streams}concat=n={len(clips)}:v=1:a=0[out]"),
                *("-map", "[out]"),
                *self._encoding(),
                partial,
            )

    async def _join(self, clips: Mapping[BotName, Path], output: Path) -> None:
        """Write the combined clip: each bot's title card, then its clip, in order."""
        with tempfile.TemporaryDirectory(prefix="iterplay-cards-") as tmp:
            sequence = list(clips.values())
            if self._config.title_card_s > 0:
                cards = {bot: Path(tmp) / f"{bot}.mp4" for bot in clips}
                for bot, card in cards.items():
                    await self._title_card(bot, card)
                sequence = [path for bot, clip in clips.items() for path in (cards[bot], clip)]
            await self._concatenate(sequence, output)

    def _fit(self) -> str:
        """Filters that resample a video to the clip rate and letterbox it into the frame."""
        width, height, fps = self._config.width, self._config.height, self._config.fps
        return (
            f"fps={fps},"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        )

    def _encoding(self) -> tuple[str, ...]:
        """Output options shared by every clip, so that clips join cleanly.

        The container is always MP4, whatever the output file is called.
        """
        return (
            *("-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"),
            *("-r", str(self._config.fps), "-an", "-movflags", "+faststart", "-f", "mp4"),
        )


def _clock_label(bot: BotName, second: int) -> str:
    """The label shown during one second of a bot's clip, e.g. ``SPEEDRUNNER 00:42``."""
    minutes, seconds = divmod(second, 60)
    return f"{bot.value.upper()} {minutes:02d}:{seconds:02d}"


def _write_clock(directory: Path, bot: BotName, seconds: int) -> None:
    """Write the clock labels, padded to one length so every image has the same size."""
    texts = [_clock_label(bot, second) for second in range(seconds)]
    length = max(len(text) for text in texts)
    for second, text in enumerate(texts):
        image = text_images.render_label(text.ljust(length), scale=LABEL_SCALE)
        (directory / (_CLOCK_FRAME % second)).write_bytes(image)


def _clock_pattern(directory: Path) -> str:
    """The image2 pattern for the clock labels, with any ``%`` in the directory escaped."""
    return str(Path(str(directory).replace("%", "%%")) / _CLOCK_FRAME)


def _write_title_card(path: Path, bot: BotName, width: int, height: int) -> None:
    path.write_bytes(text_images.render_title_card(bot.value.upper(), width=width, height=height))


def _write_labels(labels: Iterable[tuple[Path, str]]) -> None:
    for path, text in labels:
        path.write_bytes(text_images.render_label(text, scale=LABEL_SCALE))


@contextlib.asynccontextmanager
async def _replacing(output: Path) -> AsyncIterator[Path]:
    """Yield a hidden sibling of ``output`` for ffmpeg to write, renamed onto it on success.

    A failed or cancelled encode never leaves a truncated file under the final name.
    """
    partial = output.with_name(f".{output.stem}.partial{output.suffix}")
    await asyncio.to_thread(output.parent.mkdir, parents=True, exist_ok=True)
    try:
        yield partial
        await asyncio.to_thread(partial.replace, output)
    finally:
        await asyncio.to_thread(partial.unlink, missing_ok=True)
