"""Reading and writing a version's ``game/`` directory (HLD §3.1, §9)."""

from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final

from iterplay.models.game import GameBundle

VENDOR_ASSETS: Final[Traversable] = files("iterplay") / "assets" / "vendor"
"""Vendored Three.js and its MIT license, copied into every version's ``game/vendor/``."""


def write_game(game_dir: Path, bundle: GameBundle) -> None:
    """Replace the contents of ``game_dir`` with ``bundle`` plus the vendored Three.js.

    Every path is checked again with :func:`resolve_within` as defence in depth, even though
    the bundle has already passed the guardrails.

    Raises:
        UnsafePathError: A path would land outside ``game_dir``.
    """
    raise NotImplementedError


def read_game(game_dir: Path) -> GameBundle:
    """Load a version's generated files (everything except ``vendor/``) for the Fixer."""
    raise NotImplementedError


def resolve_within(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root``, refusing absolute paths, ``..`` and symlink escapes.

    Raises:
        UnsafePathError: The resolved path is not inside ``root``.
    """
    raise NotImplementedError
