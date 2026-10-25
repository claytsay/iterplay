"""Loopback-only static file serving for game builds and the viewer (HLD §2, §9).

Generated game code is untrusted, so these servers must never be reachable from the venue
network. They always bind to 127.0.0.1, and the host is deliberately not configurable.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Self

LOOPBACK_HOST: Final = "127.0.0.1"


@dataclass(frozen=True, slots=True)
class Mount:
    """Serve the files in ``directory`` under the URL prefix ``url_prefix``."""

    url_prefix: str
    directory: Path

    allowed_suffixes: frozenset[str] | None = None
    """If set, only files with these suffixes are served, e.g. ``{".json", ".mp4"}``."""


class LoopbackHTTPServer:
    """A threaded ``http.server`` on 127.0.0.1 serving one or more mounts.

    Requests are confined to their mount's directory (no ``..`` or symlink escapes),
    directory listings and dotfiles are never served, and every response carries
    ``Cache-Control: no-store`` so a browser never shows a stale version of a game.
    """

    def __init__(self, mounts: Sequence[Mount], *, port: int) -> None:
        self._mounts = tuple(mounts)
        self._port = port

    @property
    def base_url(self) -> str:
        """``http://127.0.0.1:<port>/``. With ``port=0``, the OS-assigned port once started."""
        raise NotImplementedError

    def start(self) -> None:
        """Bind, then serve on a background daemon thread."""
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def serve_forever(self) -> None:
        """Bind, then serve on the calling thread until interrupted."""
        raise NotImplementedError

    def __enter__(self) -> Self:
        """Start serving in the background."""
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop serving."""
        raise NotImplementedError
