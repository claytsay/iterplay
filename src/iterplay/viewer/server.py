"""Server behind ``iterplay view`` (HLD §7)."""

from pathlib import Path
from typing import Final

from iterplay.serving import LoopbackHTTPServer

ARTIFACT_SUFFIXES: Final = frozenset({".json", ".md", ".mp4", ".webm"})
"""Run artifacts the viewer may fetch. Generated game code is never served by the viewer."""


def viewer_server(runs_dir: Path, port: int) -> LoopbackHTTPServer:
    """Serve ``static/viewer.html`` at ``/`` and the artifacts of ``runs_dir`` at ``/runs/``.

    Not started until used.
    """
    raise NotImplementedError
