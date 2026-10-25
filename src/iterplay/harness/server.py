"""Local game server (HLD §2): one version's ``game/`` directory on 127.0.0.1."""

from pathlib import Path

from iterplay.serving import LoopbackHTTPServer


def game_server(game_dir: Path, port: int) -> LoopbackHTTPServer:
    """A server for ``game_dir`` mounted at ``/``. Not started until used."""
    raise NotImplementedError
