"""Reading and writing a version's ``game/`` directory (HLD §3.1, §9)."""

import shutil
from collections.abc import Iterator
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Final

from iterplay.contract.layout import THREE_MODULE_PATH, VENDOR_DIR
from iterplay.errors import GuardrailError, UnsafePathError
from iterplay.models.game import GameBundle, GameFile

VENDOR_ASSETS: Final[Traversable] = files("iterplay") / "assets" / "vendor"
"""Vendored Three.js and its MIT license, copied into every version's ``game/vendor/``."""

_VENDOR_LICENSE: Final = "LICENSE"
_REQUIRED_VENDOR_FILES: Final = frozenset({PurePosixPath(THREE_MODULE_PATH).name, _VENDOR_LICENSE})


def write_game(game_dir: Path, bundle: GameBundle) -> None:
    """Replace the contents of ``game_dir`` with ``bundle`` plus the vendored Three.js.

    Every path is checked again with :func:`resolve_within` as defence in depth, even though
    the bundle has already passed the guardrails. Paths that are unsafe on their face are
    refused before anything is deleted. Existing entries are removed without following
    symlinks, then each file is written as UTF-8 and every ``*.js`` file in
    :data:`VENDOR_ASSETS`, plus its ``LICENSE``, is copied into ``game_dir/vendor/``.

    Raises:
        UnsafePathError: A path would land outside ``game_dir``, or ``game_dir`` is a symlink.
        GuardrailError: A path is inside the reserved ``vendor/`` directory.
        FileNotFoundError: Three.js has not been vendored (see ``assets/vendor/README.md``).
    """
    for file in bundle.files:
        _check_relative(file.path)
        _check_not_vendor(file.path)
    assets = _vendor_assets()
    _empty_directory(game_dir)
    for file in bundle.files:
        target = resolve_within(game_dir, file.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file.content.encode("utf-8"))
    vendor_dir = game_dir / VENDOR_DIR
    vendor_dir.mkdir()
    for asset in assets:
        (vendor_dir / asset.name).write_bytes(asset.read_bytes())


def read_game(game_dir: Path) -> GameBundle:
    """Load a version's generated files (everything except ``vendor/``) for the Fixer.

    Paths are POSIX and relative to ``game_dir``, and the files are sorted by path. Symlinks
    and hidden files such as ``.DS_Store`` are skipped, because no generated file can be one.

    Raises:
        FileNotFoundError: ``game_dir`` does not exist.
        GuardrailError: A file is not UTF-8 text.
    """
    found: list[GameFile] = []
    for path in _generated_files(game_dir):
        try:
            content = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GuardrailError(
                f"{path} is not UTF-8 text (invalid byte at offset {exc.start})"
            ) from None
        found.append(GameFile(path=path.relative_to(game_dir).as_posix(), content=content))
    return GameBundle(files=sorted(found, key=lambda file: file.path))


def resolve_within(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root``, refusing absolute paths, ``..`` and symlink escapes.

    ``relative`` is a POSIX path: empty paths, backslashes and drive letters are refused too.
    Symlinks are resolved, and the result must lie strictly inside ``root.resolve()``.

    Raises:
        UnsafePathError: The resolved path is not inside ``root``.
    """
    _check_relative(relative)
    base = root.resolve()
    try:
        target = (base / relative).resolve()
    except (OSError, RuntimeError) as exc:
        raise UnsafePathError(f"Cannot resolve {relative!r} under {root}: {exc}") from exc
    if target == base or not target.is_relative_to(base):
        raise UnsafePathError(f"Unsafe path {relative!r}: it resolves outside {root}")
    return target


def _check_relative(relative: str) -> None:
    """Refuse paths that are unsafe on their face, before the file system is consulted."""
    problem = _lexical_problem(relative)
    if problem is not None:
        raise UnsafePathError(f"Unsafe path {relative!r}: {problem}")


def _lexical_problem(relative: str) -> str | None:
    if not relative:
        return "it is empty"
    if "\x00" in relative:
        return "it contains a NUL character"
    if "\\" in relative:
        return "it contains a backslash; separate directories with /"
    if relative.startswith("/") or PureWindowsPath(relative).drive:
        return "it is absolute or names a drive; give a path relative to the game directory"
    if ".." in relative.split("/"):
        return "it contains a '..' segment"
    return None


def _check_not_vendor(relative: str) -> None:
    parts = [part for part in relative.split("/") if part not in ("", ".")]
    if parts and parts[0].casefold() == VENDOR_DIR:
        raise GuardrailError(
            f"{relative!r} is inside {VENDOR_DIR}/, which is reserved for iterplay's Three.js"
        )


def _vendor_assets() -> list[Traversable]:
    assets = [
        entry
        for entry in VENDOR_ASSETS.iterdir()
        if entry.is_file() and (entry.name.endswith(".js") or entry.name == _VENDOR_LICENSE)
    ]
    missing = _REQUIRED_VENDOR_FILES - {asset.name for asset in assets}
    if missing:
        raise FileNotFoundError(
            f"Three.js is not vendored: {VENDOR_ASSETS} lacks {', '.join(sorted(missing))}. "
            "See the README.md there."
        )
    return assets


def _empty_directory(directory: Path) -> None:
    """Create ``directory`` if needed and delete its entries, never following symlinks."""
    if directory.is_symlink():
        raise UnsafePathError(f"{directory} is a symlink; refusing to replace what it points to")
    directory.mkdir(parents=True, exist_ok=True)
    for entry in directory.iterdir():
        if not entry.is_symlink() and entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _generated_files(game_dir: Path) -> Iterator[Path]:
    for entry in game_dir.iterdir():
        if entry.name != VENDOR_DIR:
            yield from _regular_files(entry)


def _regular_files(entry: Path) -> Iterator[Path]:
    """``entry`` if it is a regular file, or every regular file below it if it is a directory."""
    if entry.name.startswith(".") or entry.is_symlink():
        return
    if entry.is_dir():
        for child in entry.iterdir():
            yield from _regular_files(child)
    elif entry.is_file():
        yield entry
