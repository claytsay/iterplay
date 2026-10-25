"""Local game server (HLD §2): one version's ``game/`` directory on 127.0.0.1."""

from pathlib import Path
from typing import Final

from iterplay.contract.layout import VENDOR_DIR
from iterplay.serving import LoopbackHTTPServer, Mount, resource_dir
from iterplay.storage.game_files import VENDOR_ASSETS

_VENDOR_SUFFIXES: Final = frozenset({".js"})


def game_server(game_dir: Path, port: int) -> LoopbackHTTPServer:
    """A server for ``game_dir`` mounted at ``/``. Not started until used.

    A game directory without ``vendor/``, such as ``examples/reference_game``, is also given
    iterplay's vendored Three.js at ``/vendor/`` (its ``.js`` files only), so it can be played
    without copying Three.js in.
    """
    mounts = [Mount("/", game_dir)]
    if not (game_dir / VENDOR_DIR).is_dir():
        vendor = resource_dir(VENDOR_ASSETS)
        mounts.append(Mount(f"/{VENDOR_DIR}/", vendor, allowed_suffixes=_VENDOR_SUFFIXES))
    return LoopbackHTTPServer(mounts, port=port)
