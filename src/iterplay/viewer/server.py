"""Server behind ``iterplay view`` (HLD §7)."""

from importlib.resources import files
from pathlib import Path
from typing import Final

from iterplay.serving import LoopbackHTTPServer, Mount, resource_dir

ARTIFACT_SUFFIXES: Final = frozenset({".json", ".md", ".mp4", ".webm"})
"""Run artifacts the viewer may fetch. Generated game code is never served by the viewer."""

_PAGE: Final = "viewer.html"
_PAGE_SUFFIXES: Final = frozenset({".html", ".js", ".css"})


def viewer_server(runs_dir: Path, port: int) -> LoopbackHTTPServer:
    """Serve ``static/viewer.html`` at ``/`` and the artifacts of ``runs_dir`` at ``/runs/``.

    Not started until used. ``/`` serves only the page's own HTML, JavaScript and CSS, and
    ``/runs/`` only files with :data:`ARTIFACT_SUFFIXES`; neither lists directories.
    """
    static = resource_dir(files("iterplay.viewer") / "static")
    return LoopbackHTTPServer(
        [
            Mount("/", static, allowed_suffixes=_PAGE_SUFFIXES, index_file=_PAGE),
            Mount("/runs/", runs_dir, allowed_suffixes=ARTIFACT_SUFFIXES, index_file=None),
        ],
        port=port,
    )
