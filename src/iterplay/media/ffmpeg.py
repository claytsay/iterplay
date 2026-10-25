"""Async wrapper over the ffmpeg and ffprobe command-line tools."""

from pathlib import Path


class FFmpeg:
    """Runs ffmpeg and ffprobe as subprocesses with argument lists, never through a shell."""

    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> None:
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe

    async def run(self, *args: str | Path) -> None:
        """Run ``ffmpeg -y -hide_banner <args>``.

        Raises:
            MediaError: ffmpeg exited non-zero; the message carries the tail of stderr.
        """
        raise NotImplementedError

    async def duration_s(self, path: Path) -> float:
        """Duration of a media file, according to ffprobe."""
        raise NotImplementedError
