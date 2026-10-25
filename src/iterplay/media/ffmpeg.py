"""Async wrapper over the ffmpeg and ffprobe command-line tools."""

import asyncio
import contextlib
import logging
import math
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from iterplay.errors import MediaError

STDERR_TAIL_LINES: Final = 20
"""Lines at the end of stderr quoted in a ``MediaError``."""

_log = logging.getLogger(__name__)


class FFmpeg:
    """Runs ffmpeg and ffprobe as subprocesses with argument lists, never through a shell."""

    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> None:
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe

    async def run(self, *args: str | Path) -> None:
        """Run ``ffmpeg -y -hide_banner -loglevel error <args>``.

        Raises:
            MediaError: ffmpeg is missing or exited non-zero; the message carries the tail of
                stderr.
        """
        await _execute([self._ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *map(str, args)])

    async def duration_s(self, path: Path) -> float:
        """Duration of a media file, according to ffprobe.

        Raises:
            MediaError: ffprobe is missing or failed, or the file reports no valid duration.
        """
        options = ["-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
        stdout = await _execute([self._ffprobe, *options], capture_stdout=True)
        reported = stdout.decode(errors="replace").strip()
        try:
            duration = float(reported)
        except ValueError:
            raise MediaError(f"ffprobe reported no duration for {path}: {reported!r}") from None
        if not math.isfinite(duration) or duration < 0:
            raise MediaError(f"ffprobe reported an invalid duration for {path}: {reported!r}")
        return duration


async def _execute(command: Sequence[str], *, capture_stdout: bool = False) -> bytes:
    """Run ``command`` to completion and return its stdout, which is empty unless captured.

    A cancelled caller kills the process rather than leaving it running.

    Raises:
        MediaError: The program could not be started or exited non-zero.
    """
    program = command[0]
    _log.debug("running %s", shlex.join(command))
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise MediaError(f"{program} was not found; is ffmpeg installed?") from error
    except OSError as error:
        raise MediaError(f"could not start {program}: {error}") from error
    try:
        stdout, stderr = await process.communicate()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        raise MediaError(f"{program} exited with status {process.returncode}{_tail(stderr)}")
    return stdout or b""


def _tail(stderr: bytes) -> str:
    """The last ``STDERR_TAIL_LINES`` lines of stderr, introduced by a colon, if any."""
    lines = stderr.decode(errors="replace").strip().splitlines()[-STDERR_TAIL_LINES:]
    return ":\n" + "\n".join(lines) if lines else ""
